"""Unit tests for transient local Canvas connection failures."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from typing import Any, Self

import aiohttp

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "meural"
PACKAGE_NAME = "meural_local_api_tests"

package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(COMPONENT_PATH)]
sys.modules[PACKAGE_NAME] = package

try:
    from homeassistant.exceptions import HomeAssistantError
except ImportError:
    homeassistant = types.ModuleType("homeassistant")
    exceptions = types.ModuleType("homeassistant.exceptions")

    class HomeAssistantError(Exception):
        """Minimal Home Assistant error stub for isolated client tests."""

    exceptions.HomeAssistantError = HomeAssistantError
    homeassistant.exceptions = exceptions
    sys.modules.setdefault("homeassistant", homeassistant)
    sys.modules.setdefault("homeassistant.exceptions", exceptions)

MODULE_PATH = COMPONENT_PATH / "pymeural.py"
SPEC = importlib.util.spec_from_file_location(f"{PACKAGE_NAME}.pymeural", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
pymeural = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pymeural
SPEC.loader.exec_module(pymeural)


class FakeResponse:
    """Minimal aiohttp response context manager."""

    def __init__(self, body: Any) -> None:
        self.body = body

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def json(self, content_type=None) -> Any:
        return self.body


class RaisingRequest:
    """Request context manager that fails while connecting."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def __aenter__(self) -> None:
        raise self.error

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakeSession:
    """Queue request contexts and capture outgoing options."""

    def __init__(self, *outcomes: FakeResponse | RaisingRequest) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.requests.append({"method": method, "url": url, **kwargs})
        if not self.outcomes:
            raise AssertionError(f"Unexpected request: {method} {url}")
        return self.outcomes.pop(0)


class LocalMeuralRequestTest(unittest.IsolatedAsyncioTestCase):
    """Exercise retry and connection handling for the local Canvas API."""

    def setUp(self) -> None:
        pymeural.LOCAL_RETRY_DELAY = 0

    async def test_safe_read_retries_after_server_disconnect(self) -> None:
        session = FakeSession(
            RaisingRequest(aiohttp.ServerDisconnectedError()),
            FakeResponse({"response": False}),
        )
        client = pymeural.LocalMeural(
            {"localIp": "192.0.2.10", "alias": "Test Canvas"}, session
        )

        sleeping = await client.send_get_sleep()

        self.assertFalse(sleeping)
        self.assertEqual(2, len(session.requests))
        self.assertEqual("close", session.requests[0]["headers"]["Connection"])

    async def test_safe_read_raises_after_second_interruption(self) -> None:
        session = FakeSession(
            RaisingRequest(aiohttp.ServerDisconnectedError()),
            RaisingRequest(aiohttp.ClientOSError(104, "Connection reset by peer")),
        )
        client = pymeural.LocalMeural(
            {"localIp": "192.0.2.10", "alias": "Test Canvas"}, session
        )

        with self.assertRaises(aiohttp.ClientOSError):
            await client.send_get_system()

        self.assertEqual(2, len(session.requests))

    async def test_control_command_is_not_retried(self) -> None:
        session = FakeSession(RaisingRequest(aiohttp.ServerDisconnectedError()))
        client = pymeural.LocalMeural(
            {"localIp": "192.0.2.10", "alias": "Test Canvas"}, session
        )

        with self.assertRaises(aiohttp.ServerDisconnectedError):
            await client.send_key_right()

        self.assertEqual(1, len(session.requests))

    async def test_device_update_uses_new_cloud_ip(self) -> None:
        session = FakeSession(FakeResponse({"response": True}))
        client = pymeural.LocalMeural(
            {"localIp": "192.0.2.10", "alias": "Test Canvas"}, session
        )

        changed = client.update_device(
            {"localIp": "192.0.2.25", "alias": "Test Canvas"}
        )
        sleeping = await client.send_get_sleep()

        self.assertTrue(changed)
        self.assertTrue(sleeping)
        self.assertEqual("192.0.2.25", client.ip)
        self.assertIn("http://192.0.2.25/", session.requests[0]["url"])

    async def test_device_update_reports_unchanged_ip(self) -> None:
        session = FakeSession()
        client = pymeural.LocalMeural(
            {"localIp": "192.0.2.10", "alias": "Test Canvas"}, session
        )

        changed = client.update_device(
            {"localIp": "192.0.2.10", "alias": "Renamed Canvas"}
        )

        self.assertFalse(changed)
        self.assertEqual("Renamed Canvas", client.device["alias"])


if __name__ == "__main__":
    unittest.main()
