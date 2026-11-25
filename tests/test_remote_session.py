"""Remote session lifecycle with the real gate/server/broker and fake OS edges (banner, tunnel, capture, input)."""

from __future__ import annotations

import asyncio
import re
import unittest
from unittest import mock

from _bootstrap import PROJECT_DIR
assert PROJECT_DIR.is_dir()
from _remote_helpers import http_request, ws_open
from assistant.remote import session as session_mod
from assistant.remote.inputs import RecordingBackend
from assistant.remote.session import RemoteController, RemoteDeps, RemoteSession
from assistant.remote.stream import JpegFrame

OWNER = 42
OTHER = 43
TINY_JPEG = b"\xff\xd8\xff\xd9"
LINK_RE = re.compile(r"(https://[a-z0-9-]+\.trycloudflare\.com)/connect/([A-Za-z0-9_\-]+)")


class FakeBanner:
    def __init__(self, on_stop) -> None:
        self.on_stop = on_stop
        self.lines: list[str] = []
        self.is_visible = True
        self.closed = False

    def send(self, line: str) -> None:
        self.lines.append(line)

    def visible(self, max_age: float = 3.0) -> bool:
        return self.is_visible

    async def close(self) -> None:
        self.closed = True


class FakeTunnel:
    instances: list["FakeTunnel"] = []
    url = "https://fakeone-test.trycloudflare.com"
    give_url = True

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.is_alive = True
        self.closed = False

    @classmethod
    def spawn(cls, provider: str, argv: list[str]) -> "FakeTunnel":
        tunnel = cls(provider)
        cls.instances.append(tunnel)
        return tunnel

    async def wait_for_url(self, timeout: float) -> str | None:
        return self.url if self.give_url else None

    def alive(self) -> bool:
        return self.is_alive and not self.closed

    def close(self) -> None:
        self.closed = True


def fake_capture(quality: int) -> JpegFrame:
    return JpegFrame(TINY_JPEG, 2, 2)


class Harness:
    """Builds deps and collects everything the session sends to Telegram."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.banner: FakeBanner | None = None
        self.messages: list[str] = []
        self.backend = RecordingBackend((0, 0, 1000, 500))
        self.fail_link = False
        self.banner_error: Exception | None = None

        async def open_banner(on_stop):
            if self.banner_error is not None:
                raise self.banner_error
            self.banner = FakeBanner(on_stop)
            return self.banner

        async def say(text: str) -> None:
            self.messages.append(text)

        async def send_link(text: str) -> None:
            if self.fail_link:
                raise RuntimeError("telegram down")
            self.messages.append(text)

        self.deps = RemoteDeps(
            open_banner=open_banner,
            tunnel_argv=lambda provider, port: ["fake-tunnel", str(port)],
            capture=fake_capture,
            input_backend=lambda: self.backend,
            say=say,
            send_link=send_link,
            providers=("cloudflare",),
        )

    def link(self) -> tuple[str, str] | None:
        for text in self.messages:
            match = LINK_RE.search(text)
            if match:
                return match.group(1), match.group(2)
        return None


class FakeOsTestCase(unittest.IsolatedAsyncioTestCase):
    """Replaces the tunnel process and the watchdog cadence; never spawns cloudflared/ssh."""

    def setUp(self) -> None:
        FakeTunnel.instances = []
        FakeTunnel.give_url = True
        self.patches = [
            mock.patch.object(session_mod, "TunnelProcess", FakeTunnel),
            mock.patch.object(session_mod, "WATCHDOG_INTERVAL", 0.05),
        ]
        for patch in self.patches:
            patch.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])


class RemoteSessionTests(FakeOsTestCase):

    async def start_session(self, minutes: int = 1, duration: float | None = None):
        harness = Harness(self)
        session = RemoteSession(harness.deps, user_id=OWNER, minutes=minutes, duration_seconds=duration)
        ok, message = await session.start()
        self.addAsyncCleanup(session.stop, "requested")
        return harness, session, ok, message

    async def test_start_sends_one_private_link_and_activates(self):
        harness, session, ok, _ = await self.start_session()
        self.assertTrue(ok)
        self.assertEqual(session.state, "active")
        self.assertIsNotNone(harness.banner)
        link = harness.link()
        self.assertIsNotNone(link, "the link must arrive in Telegram")
        self.assertEqual(link[0], FakeTunnel.url)
        self.assertGreaterEqual(len(link[1]), 43)
        self.assertIn("FULL VIEW and CONTROL", harness.messages[0])
        self.assertIn("for 1 minute(s)", harness.messages[0])

    async def test_secret_never_reaches_the_audit_or_log(self):
        with self.assertLogs(level="DEBUG") as logs:
            harness, session, _, _ = await self.start_session()
            link = harness.link()
            await session.stop("requested")
        self.assertIsNotNone(link)
        secret = link[1]
        for record in logs.output:
            self.assertNotIn(secret, record)
            self.assertNotIn("trycloudflare", record)

    async def test_viewer_connect_is_a_one_time_locked_session_over_real_http(self):
        harness, session, _, _ = await self.start_session()
        host, secret = harness.link()
        port = session.server.port
        status, headers, _ = await http_request(port, "GET", f"/connect/{secret}",
                                                host=host.split("//")[1])
        self.assertEqual(status, 303)
        self.assertIn("pcr_session=", headers["set-cookie"])
        self.assertIn("HttpOnly", headers["set-cookie"])
        cookie = headers["set-cookie"].split("pcr_session=")[1].split(";")[0]
        again, _, _ = await http_request(port, "GET", f"/connect/{secret}", host=host.split("//")[1])
        self.assertEqual(again, 403)
        ok, _, _, writer = await ws_open(port, cookie, origin=host, host=host.split("//")[1])
        self.assertEqual(ok, 101)
        writer.close()

    async def test_requests_without_cookie_or_secret_get_403(self):
        harness, session, _, _ = await self.start_session()
        port = session.server.port
        self.assertEqual((await http_request(port, "GET", "/"))[0], 403)
        self.assertEqual((await http_request(port, "GET", "/connect/wrong"))[0], 403)
        self.assertEqual((await http_request(port, "GET", "/ws"))[0], 403)

    async def test_stop_revokes_the_link_and_kills_everything(self):
        harness, session, _, _ = await self.start_session()
        host, secret = harness.link()
        port = session.server.port
        tunnel = FakeTunnel.instances[-1]
        await session.stop("requested")
        self.assertEqual(session.state, "stopped")
        self.assertTrue(tunnel.closed)
        self.assertTrue(harness.banner.closed)
        self.assertIsNone(session.server)
        self.assertIn("no longer works", harness.messages[-1])
        with self.assertRaises(OSError):
            await asyncio.open_connection("127.0.0.1", port)

    async def test_stop_is_idempotent_and_safe_to_repeat(self):
        harness, session, _, _ = await self.start_session()
        await asyncio.gather(session.stop("requested"), session.stop("panic"), session.stop("button"))
        await session.stop("requested")
        stopped_messages = [m for m in harness.messages if "stopped" in m.lower()]
        self.assertEqual(len(stopped_messages), 1, "exactly one stop notice")

    async def test_timeout_stops_on_its_own(self):
        harness, session, _, _ = await self.start_session(duration=0.2)
        for _ in range(100):
            if session.state == "stopped":
                break
            await asyncio.sleep(0.02)
        self.assertEqual(session.state, "stopped")
        self.assertEqual(session.reason, "timeout")

    async def test_banner_stop_button_and_hotkey_end_the_session(self):
        for reason, expected in (("button", "button"), ("hotkey", "hotkey")):
            with self.subTest(reason=reason):
                harness, session, _, _ = await self.start_session()
                harness.banner.on_stop(reason)
                for _ in range(100):
                    if session.state == "stopped":
                        break
                    await asyncio.sleep(0.02)
                self.assertEqual(session.state, "stopped")
                self.assertEqual(session.reason, expected)

    async def test_banner_going_silent_stops_the_session(self):
        harness, session, _, _ = await self.start_session()
        harness.banner.is_visible = False
        for _ in range(100):
            if session.state == "stopped":
                break
            await asyncio.sleep(0.02)
        self.assertEqual(session.reason, "banner lost")

    async def test_tunnel_dying_stops_the_session(self):
        harness, session, _, _ = await self.start_session()
        FakeTunnel.instances[-1].is_alive = False
        for _ in range(100):
            if session.state == "stopped":
                break
            await asyncio.sleep(0.02)
        self.assertEqual(session.reason, "tunnel closed")

    async def test_failed_banner_means_nothing_is_exposed(self):
        harness = Harness(self)
        harness.banner_error = session_mod.RemoteError("banner did not start")
        session = RemoteSession(harness.deps, user_id=OWNER, minutes=1)
        ok, message = await session.start()
        self.assertFalse(ok)
        self.assertEqual(session.state, "stopped")
        self.assertEqual(harness.messages, [])
        self.assertIsNone(session.server)

    async def test_no_tunnel_means_nothing_is_exposed(self):
        FakeTunnel.give_url = False
        harness = Harness(self)
        session = RemoteSession(harness.deps, user_id=OWNER, minutes=1)
        ok, message = await session.start()
        self.assertFalse(ok)
        self.assertIn("tunnel", message.lower())
        self.assertEqual(harness.messages, [])
        self.assertTrue(FakeTunnel.instances[-1].closed)
        self.assertEqual(session.state, "stopped")

    async def test_failed_link_delivery_stops_and_does_not_leave_a_server(self):
        harness = Harness(self)
        harness.fail_link = True
        session = RemoteSession(harness.deps, user_id=OWNER, minutes=1)
        ok, _ = await session.start()
        self.assertFalse(ok)
        self.assertEqual(session.state, "stopped")
        self.assertIsNone(session.server)
        self.assertTrue(FakeTunnel.instances[-1].closed)

    async def test_session_can_only_start_once(self):
        harness, session, _, _ = await self.start_session()
        ok, message = await session.start()
        self.assertFalse(ok)
        self.assertIn("already started", message)

    async def test_viewer_connection_notifies_telegram(self):
        harness, session, _, _ = await self.start_session()
        session._on_viewer(True)
        await asyncio.sleep(0.05)
        self.assertTrue(any("viewer connected" in m.lower() for m in harness.messages))
        self.assertIn("VIEWER connected", harness.banner.lines)

    async def test_capture_only_runs_while_a_viewer_is_connected(self):
        calls = []

        def counting_capture(quality):
            calls.append(quality)
            return JpegFrame(TINY_JPEG, 2, 2)

        harness = Harness(self)
        harness.deps.capture = counting_capture
        session = RemoteSession(harness.deps, user_id=OWNER, minutes=1)
        await session.start()
        self.addAsyncCleanup(session.stop, "requested")
        await asyncio.sleep(0.2)
        self.assertEqual(calls, [], "no capture before a viewer connects")
        session._on_viewer(True)
        await asyncio.sleep(0.25)
        self.assertGreater(len(calls), 0)
        session._on_viewer(False)
        before = len(calls)
        await asyncio.sleep(0.25)
        self.assertLessEqual(len(calls) - before, 1)

    async def test_input_is_applied_only_while_active_and_rate_limited(self):
        harness, session, _, _ = await self.start_session()
        session._on_input({"k": "move", "x": 0.5, "y": 0.5})
        self.assertEqual(harness.backend.calls[-1][0], "move")
        session.limiter._burst = 0.0
        session.limiter._tokens = 0.0
        before = session.inputs_rejected
        session._on_input({"k": "move", "x": 0.1, "y": 0.1})
        self.assertEqual(session.inputs_rejected, before + 1)
        await session.stop("requested")
        applied = len(harness.backend.calls)
        session._on_input({"k": "move", "x": 0.2, "y": 0.2})
        self.assertEqual(len(harness.backend.calls), applied)

    async def test_stop_releases_held_keys_and_buttons(self):
        harness, session, _, _ = await self.start_session()
        session._on_input({"k": "mdown", "x": 0.5, "y": 0.5, "b": 0})
        session._on_input({"k": "key", "down": True, "key": "a", "code": "KeyA"})
        self.assertIn(("mouse", 0, True), harness.backend.calls)
        self.assertIn(("unicode", "a", True), harness.backend.calls)
        await session.stop("requested")
        self.assertIn(("mouse", 0, False), harness.backend.calls)
        self.assertIn(("unicode", "a", False), harness.backend.calls)

    async def test_stats_show_remaining_time_without_secrets(self):
        harness, session, _, _ = await self.start_session(minutes=2)
        stats = session._stats()
        self.assertLessEqual(stats["remaining"], 120)
        self.assertGreater(stats["remaining"], 100)
        self.assertNotIn("secret", str(stats).lower())
        self.assertNotIn(harness.link()[1], str(stats))


class ControllerAuthorizationTests(FakeOsTestCase):
    def make_controller(self, *, platform_ok: bool = True, max_minutes: int = 60, harness=None):
        harness = harness or Harness(self)
        return RemoteController(
            is_allowed=lambda uid: uid == OWNER,
            max_minutes=max_minutes, default_minutes=15,
            deps_factory=lambda chat_id: harness.deps,
            platform_ok=lambda: platform_ok,
        ), harness

    async def test_only_the_allowed_private_chat_can_request_a_session(self):
        controller, _ = self.make_controller()
        self.assertIsNone(controller.check_request(OWNER, OWNER, 15))
        self.assertIsNotNone(controller.check_request(OTHER, OTHER, 15))
        self.assertIsNotNone(controller.check_request(OWNER, -100, 15), "group chats are refused")

    async def test_duration_must_be_in_range_and_default_is_15(self):
        controller, _ = self.make_controller()
        self.assertIsNotNone(controller.check_request(OWNER, OWNER, 0))
        self.assertIsNotNone(controller.check_request(OWNER, OWNER, 61))
        self.assertIsNone(controller.check_request(OWNER, OWNER, 60))
        self.assertEqual(controller.default_minutes, 15)
        self.assertEqual(controller.max_minutes, 60)

    async def test_refused_on_non_windows(self):
        controller, _ = self.make_controller(platform_ok=False)
        self.assertIn("Windows", controller.check_request(OWNER, OWNER, 5))

    async def test_only_one_session_at_a_time(self):
        controller, harness = self.make_controller()
        ok, _ = await controller.start(OWNER, OWNER, 1)
        self.assertTrue(ok)
        self.addAsyncCleanup(controller.stop, "requested")
        self.assertIn("already running", controller.check_request(OWNER, OWNER, 1))
        ok2, message = await controller.start(OWNER, OWNER, 1)
        self.assertFalse(ok2)

    async def test_start_refused_for_outsider_creates_no_session(self):
        controller, harness = self.make_controller()
        ok, _ = await controller.start(OTHER, OTHER, 5)
        self.assertFalse(ok)
        self.assertIsNone(controller.active)
        self.assertEqual(harness.messages, [])
        self.assertIsNone(harness.banner)

    async def test_panic_path_stops_any_running_session(self):
        controller, harness = self.make_controller()
        await controller.start(OWNER, OWNER, 5)
        self.assertTrue(controller.running())
        self.assertTrue(await controller.stop("panic"))
        self.assertFalse(controller.running())
        self.assertIn("No remote session is running", controller.status_text())

    async def test_status_text_has_no_link_or_secret(self):
        controller, harness = self.make_controller()
        await controller.start(OWNER, OWNER, 5)
        self.addAsyncCleanup(controller.stop, "requested")
        status = controller.status_text()
        self.assertIn("Remote session", status)
        self.assertNotIn("connect/", status)
        self.assertNotIn(harness.link()[1], status)

    async def test_startup_timeout_leaves_nothing_open(self):
        harness = Harness(self)
        original = harness.deps.open_banner

        async def slow_banner(on_stop):
            await asyncio.sleep(5)
            return await original(on_stop)

        harness.deps.open_banner = slow_banner
        controller, _ = self.make_controller(harness=harness)
        with mock.patch.object(session_mod, "START_LIMIT", 0.1):
            ok, message = await controller.start(OWNER, OWNER, 5)
        self.assertFalse(ok)
        self.assertIsNone(controller.active)
        self.assertEqual(harness.messages, [])


if __name__ == "__main__":
    unittest.main()
