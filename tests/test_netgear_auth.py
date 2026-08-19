"""Unit tests for the Netgear Accounts authentication flow."""

from __future__ import annotations

import importlib.util
import sys
import time
import unittest
from pathlib import Path
from typing import Any, Self

MODULE_PATH = (
    Path(__file__).parents[1] / "custom_components" / "meural" / "netgear_auth.py"
)
SPEC = importlib.util.spec_from_file_location("meural_netgear_auth", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
auth = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = auth
SPEC.loader.exec_module(auth)


class FakeResponse:
    """Minimal aiohttp response for deterministic auth tests."""

    def __init__(self, body: Any, status: int = 200) -> None:
        self.body = body
        self.status = status

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def json(self, content_type=None) -> Any:
        return self.body

    async def text(self) -> str:
        return str(self.body)


class FakeSession:
    """Queue responses and capture outgoing requests."""

    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError(f"Unexpected request: {method} {url}")
        return self.responses.pop(0)


def password_challenge(session: str = "password-session") -> FakeResponse:
    """Return the first custom password challenge."""
    return FakeResponse(
        {
            "ChallengeName": "CUSTOM_CHALLENGE",
            "ChallengeParameters": {"prompt": "password"},
            "Session": session,
        }
    )


def cognito_success() -> FakeResponse:
    """Return successful Cognito tokens."""
    return FakeResponse(
        {
            "AuthenticationResult": {
                "AccessToken": "cognito-access-token",
                "IdToken": "cognito-id-token",
            }
        }
    )


def authorize_success() -> FakeResponse:
    """Return a Netgear authorization code."""
    return FakeResponse({"data": {"code": "authorization-code"}})


def meural_token_success() -> FakeResponse:
    """Return Meural OAuth tokens."""
    return FakeResponse(
        {
            "data": {
                "access_token": "meural-access-token",
                "refresh_token": "meural-refresh-token",
                "expires_in": 3600,
            }
        }
    )


class NetgearAuthenticatorTest(unittest.IsolatedAsyncioTestCase):
    """Exercise login, challenge, refresh, migration, and WAF handling."""

    async def test_password_login_and_token_exchange(self) -> None:
        session = FakeSession(
            password_challenge(),
            cognito_success(),
            authorize_success(),
            meural_token_success(),
        )
        authenticator = auth.NetgearAuthenticator(session, "trust-id")

        result = await authenticator.authenticate("person@example.com", "secret")

        self.assertEqual("meural-access-token", result.access_token)
        self.assertEqual("meural-refresh-token", result.refresh_token)
        self.assertEqual("trust-id", result.trust_id)
        self.assertEqual(
            "CUSTOM_AUTH",
            session.requests[0]["json"]["AuthFlow"],
        )
        self.assertEqual(
            "secret",
            session.requests[1]["json"]["ChallengeResponses"]["ANSWER"],
        )
        self.assertIn("/api/oauth/authorize?", session.requests[2]["url"])
        self.assertIn("/api/oauth/token?", session.requests[3]["url"])

    async def test_interactive_otp_challenge(self) -> None:
        otp_response = FakeResponse(
            {
                "ChallengeName": "CUSTOM_CHALLENGE",
                "ChallengeParameters": {
                    "deliveryDestination": "p***@example.com",
                    "prompt": "email verification code",
                },
                "Session": "otp-session",
            }
        )
        session = FakeSession(password_challenge(), otp_response)
        authenticator = auth.NetgearAuthenticator(session, "trust-id")

        with self.assertRaises(auth.ChallengeRequired) as raised:
            await authenticator.authenticate("person@example.com", "secret")

        session.responses.extend(
            [cognito_success(), authorize_success(), meural_token_success()]
        )
        result = await authenticator.complete_challenge(
            raised.exception.challenge,
            "123456",
        )

        self.assertEqual("meural-access-token", result.access_token)
        self.assertEqual(
            "123456",
            session.requests[2]["json"]["ChallengeResponses"]["ANSWER"],
        )

    async def test_challenge_transient_server_error_is_cannot_connect(self) -> None:
        """A 5xx while submitting a challenge answer must not read as a bad code."""
        otp_response = FakeResponse(
            {
                "ChallengeName": "CUSTOM_CHALLENGE",
                "ChallengeParameters": {"prompt": "email verification code"},
                "Session": "otp-session",
            }
        )
        session = FakeSession(password_challenge(), otp_response)
        authenticator = auth.NetgearAuthenticator(session, "trust-id")
        with self.assertRaises(auth.ChallengeRequired) as raised:
            await authenticator.authenticate("person@example.com", "secret")

        session.responses.append(
            FakeResponse({"message": "Internal server error"}, status=503)
        )
        with self.assertRaises(auth.CannotConnect):
            await authenticator.complete_challenge(
                raised.exception.challenge, "123456"
            )

    async def test_cloudfront_html_block_page_is_waf_error(self) -> None:
        """CloudFront answers a block with an HTML page, not a JSON error body."""
        session = FakeSession(
            FakeResponse(
                "<html><body>Request blocked by CloudFront</body></html>",
                status=403,
            )
        )
        authenticator = auth.NetgearAuthenticator(session, "trust-id")

        with self.assertRaises(auth.AuthenticationBlocked):
            await authenticator.authenticate("person@example.com", "secret")

    async def test_migration_detected_via_cognito_exception_name(self) -> None:
        """Cognito's own UserNotFoundException serialization must also fall back."""
        session = FakeSession(
            FakeResponse(
                {"__type": "UserNotFoundException", "message": "not found"},
                status=400,
            ),
            cognito_success(),
            authorize_success(),
            meural_token_success(),
        )
        authenticator = auth.NetgearAuthenticator(session, "trust-id")

        await authenticator.authenticate("person@example.com", "secret")

        self.assertEqual(
            "USER_PASSWORD_AUTH",
            session.requests[1]["json"]["AuthFlow"],
        )

    async def test_browser_cognito_token_exchange(self) -> None:
        """A browser-obtained Cognito token can finish on Home Assistant."""
        session = FakeSession(authorize_success(), meural_token_success())
        authenticator = auth.NetgearAuthenticator(session, "mobile-trust-id")

        result = await authenticator.exchange_cognito_token(
            "browser-cognito-access-token"
        )

        self.assertEqual("meural-access-token", result.access_token)
        self.assertEqual("meural-refresh-token", result.refresh_token)
        self.assertEqual("mobile-trust-id", result.trust_id)
        self.assertEqual(
            "Bearer browser-cognito-access-token",
            session.requests[0]["headers"]["Authorization"],
        )
        self.assertEqual(2, len(session.requests))

    async def test_browser_token_exchange_reports_waf_block(self) -> None:
        session = FakeSession(
            FakeResponse(
                {
                    "__type": "ForbiddenException",
                    "message": "Request not allowed due to WAF block.",
                },
                status=403,
            )
        )
        authenticator = auth.NetgearAuthenticator(session, "mobile-trust-id")

        with self.assertRaises(auth.AuthenticationBlocked):
            await authenticator.exchange_cognito_token("browser-cognito-access-token")

    async def test_refresh_uses_netgear_accounts(self) -> None:
        session = FakeSession(
            FakeResponse(
                {
                    "access_token": "rotated-access-token",
                    "expires_in": 7200,
                }
            )
        )
        authenticator = auth.NetgearAuthenticator(session, "trust-id")

        result = await authenticator.refresh("existing-refresh-token")

        self.assertEqual("rotated-access-token", result.access_token)
        self.assertEqual("existing-refresh-token", result.refresh_token)
        self.assertEqual(
            "Bearer existing-refresh-token",
            session.requests[0]["headers"]["Authorization"],
        )
        self.assertEqual(
            auth.MEURAL_OAUTH_CLIENT_ID,
            session.requests[0]["headers"]["appkey"],
        )

    async def test_waf_block_has_specific_error(self) -> None:
        session = FakeSession(
            FakeResponse(
                {
                    "__type": "ForbiddenException",
                    "message": "Request not allowed due to WAF block.",
                },
                status=400,
            )
        )
        authenticator = auth.NetgearAuthenticator(session, "trust-id")

        with self.assertRaises(auth.AuthenticationBlocked):
            await authenticator.authenticate("person@example.com", "secret")

    async def test_account_migration_falls_back_to_password_auth(self) -> None:
        session = FakeSession(
            FakeResponse(
                {
                    "__type": "UserLambdaValidationException",
                    "message": "User_Not_Found",
                },
                status=400,
            ),
            cognito_success(),
            authorize_success(),
            meural_token_success(),
        )
        authenticator = auth.NetgearAuthenticator(session, "trust-id")

        await authenticator.authenticate("person@example.com", "secret")

        self.assertEqual(
            "USER_PASSWORD_AUTH",
            session.requests[1]["json"]["AuthFlow"],
        )

    async def test_rejected_refresh_requires_reauthentication(self) -> None:
        session = FakeSession(FakeResponse({"message": "Unauthorized"}, status=401))
        authenticator = auth.NetgearAuthenticator(session, "trust-id")

        with self.assertRaises(auth.InvalidAuth):
            await authenticator.refresh("expired-refresh-token")

    async def test_waf_block_on_refresh_is_not_reported_as_invalid_auth(self) -> None:
        """A WAF block on the background refresh call must not force reauth."""
        session = FakeSession(
            FakeResponse(
                {
                    "__type": "ForbiddenException",
                    "message": "Request not allowed due to WAF block.",
                },
                status=403,
            )
        )
        authenticator = auth.NetgearAuthenticator(session, "trust-id")

        with self.assertRaises(auth.AuthenticationBlocked):
            await authenticator.refresh("existing-refresh-token")

    async def test_refresh_treats_zero_expires_in_as_expired(self) -> None:
        """expires_in: 0 must not be silently substituted with a 1-hour fallback."""
        session = FakeSession(
            FakeResponse({"access_token": "rotated-access-token", "expires_in": 0})
        )
        authenticator = auth.NetgearAuthenticator(session, "trust-id")

        result = await authenticator.refresh("existing-refresh-token")

        self.assertLessEqual(result.expires_at, time.time())


if __name__ == "__main__":
    unittest.main()
