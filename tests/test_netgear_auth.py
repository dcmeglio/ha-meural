"""Unit tests for the Netgear Accounts authentication flow."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from typing import Any

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

    async def __aenter__(self) -> "FakeResponse":
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

    async def test_temporary_proxy_routes_complete_login(self) -> None:
        session = FakeSession(
            password_challenge(),
            cognito_success(),
            authorize_success(),
            meural_token_success(),
        )
        proxy_url = "http://meural:temporary@192.168.1.10:8080"
        authenticator = auth.NetgearAuthenticator(
            session,
            "trust-id",
            proxy_url=proxy_url,
        )

        await authenticator.authenticate("person@example.com", "secret")

        self.assertEqual(4, len(session.requests))
        self.assertTrue(
            all(request["proxy"] == proxy_url for request in session.requests)
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

    def test_temporary_proxy_validation(self) -> None:
        self.assertIsNone(auth.normalize_http_proxy(""))
        self.assertEqual(
            "http://host.local:8080",
            auth.normalize_http_proxy(" http://host.local:8080 "),
        )

        for invalid_proxy in (
            "https://host.local:8080",
            "http://",
            "http://host.local:99999",
            "http://host.local:8080/path",
            "http://host.local:8080?query=value",
        ):
            with self.subTest(proxy=invalid_proxy):
                with self.assertRaises(auth.InvalidProxy):
                    auth.normalize_http_proxy(invalid_proxy)

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
        self.assertNotIn("proxy", session.requests[0])

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


if __name__ == "__main__":
    unittest.main()
