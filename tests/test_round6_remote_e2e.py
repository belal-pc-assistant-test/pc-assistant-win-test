"""End-to-end remote access over real loopback HTTP (Round 6).

A real ``RemoteSession`` starts the real ``RemoteServer`` on 127.0.0.1. Only the
OS edges are fakes (banner, tunnel process, screen capture, input backend), as
in ``test_remote_session``. Plain HTTP requests go through ``http.client`` in
worker threads, so the server's own parser is never the client. WebSocket
upgrades use the shared asyncio client, because ``http.client`` cannot speak
WebSocket frames.
"""

from __future__ import annotations

import asyncio
import http.client
import unittest
from urllib.parse import urlsplit

from _bootstrap import PROJECT_DIR
assert PROJECT_DIR.is_dir()

from _remote_helpers import client_frame, ws_open  # noqa: E402
from assistant.remote.server import COOKIE_NAME  # noqa: E402
from test_remote_session import FakeOsTestCase, Harness, OWNER  # noqa: E402
from assistant.remote.session import RemoteSession  # noqa: E402

OP_TEXT = 0x1


def _http(port: int, method: str, path: str, host: str, headers: dict | None = None):
    """One plain HTTP/1.1 request with http.client. Returns (status, headers, body)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host)
        for name, value in (headers or {}).items():
            conn.putheader(name, value)
        conn.endheaders()
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, body
    finally:
        conn.close()


class RemoteE2ETests(FakeOsTestCase):

    async def start(self, duration: float | None = None):
        harness = Harness(self)
        session = RemoteSession(harness.deps, user_id=OWNER, minutes=1, duration_seconds=duration)
        ok, message = await session.start()
        self.assertTrue(ok, message)
        self.addAsyncCleanup(session.stop, "requested")
        link = harness.link()
        self.assertIsNotNone(link, "the private link must be sent to Telegram")
        public, secret = link
        self.host = urlsplit(public).netloc
        self.secret = secret
        self.port = session.server._server.sockets[0].getsockname()[1]
        self.assertEqual(self.port, session.server.port)
        return harness, session

    async def http(self, method: str, path: str, host: str | None = None, headers: dict | None = None):
        return await asyncio.to_thread(_http, self.port, method, path, host or self.host, headers)

    async def redeem(self) -> str:
        status, headers, _ = await self.http("GET", f"/connect/{self.secret}")
        self.assertEqual(status, 303)
        return headers["set-cookie"].split(f"{COOKIE_NAME}=")[1].split(";")[0]

    async def try_http(self, *args, **kwargs):
        """After a stop the server may already be closed: a refused connection also counts as no access."""
        try:
            return (await self.http(*args, **kwargs))[0]
        except OSError:
            return None


class HostAndOriginTests(RemoteE2ETests):

    async def test_wrong_host_is_refused_and_does_not_use_up_the_link(self):
        await self.start()
        status, _, _ = await self.http("GET", f"/connect/{self.secret}", host="evil.example")
        self.assertEqual(status, 403)
        self.assertEqual((await self.http("GET", f"/connect/{self.secret}", host=self.host))[0], 303,
                         "a refused Host must not consume the one-time link")

    async def test_dns_rebinding_style_host_is_refused(self):
        await self.start()
        for host in (f"{self.host}.evil.example", f"evil.example/{self.host}", "203.0.113.9"):
            status, _, _ = await self.http("GET", f"/connect/{self.secret}", host=host)
            self.assertEqual(status, 403, host)

    async def test_loopback_names_are_accepted_by_design(self):
        # A DNS-rebinding page sends its own domain as Host, so the bare loopback
        # names do not help it. Every route still needs the cookie.
        await self.start()
        status, _, _ = await self.http("GET", f"/connect/{self.secret}", host="127.0.0.1")
        self.assertEqual(status, 303)
        self.assertEqual((await self.http("GET", "/"))[0], 403)

    async def test_default_https_port_is_the_same_host(self):
        await self.start()
        status, _, _ = await self.http("GET", f"/connect/{self.secret}", host=f"{self.host}:443")
        self.assertEqual(status, 303)

    async def test_foreign_origin_on_websocket_is_refused_and_own_origin_accepted(self):
        await self.start()
        cookie = await self.redeem()
        status, _, _, writer = await ws_open(self.port, cookie, origin="https://evil.example", host=self.host)
        writer.close()
        self.assertEqual(status, 403)
        status, _, _, writer = await ws_open(self.port, cookie, origin=f"https://{self.host}", host=self.host)
        writer.close()
        self.assertEqual(status, 101)

    async def test_null_origin_is_refused(self):
        await self.start()
        cookie = await self.redeem()
        status, _, _, writer = await ws_open(self.port, cookie, origin="null", host=self.host)
        writer.close()
        self.assertEqual(status, 403)


class LinkAndCookieTests(RemoteE2ETests):

    async def test_link_works_once_then_fails(self):
        await self.start()
        cookie = await self.redeem()
        self.assertTrue(cookie)
        status, _, _ = await self.http("GET", f"/connect/{self.secret}")
        self.assertNotEqual(status, 303, "a second use of the link must not issue another cookie")
        self.assertEqual(status, 403)

    async def test_viewer_page_needs_the_cookie(self):
        await self.start()
        self.assertEqual((await self.http("GET", "/"))[0], 403)
        cookie = await self.redeem()
        status, headers, body = await self.http("GET", "/", headers={"Cookie": f"{COOKIE_NAME}={cookie}"})
        self.assertEqual(status, 200)
        self.assertIn("content-security-policy", headers)
        self.assertIn(b"<", body)

    async def test_websocket_needs_the_cookie(self):
        await self.start()
        status, _, _, writer = await ws_open(self.port, None, host=self.host)
        writer.close()
        self.assertEqual(status, 403)


class ViewerLimitTests(RemoteE2ETests):

    async def test_only_one_viewer_at_a_time(self):
        harness, session = await self.start()
        cookie = await self.redeem()
        first, second = await asyncio.gather(
            ws_open(self.port, cookie, origin=f"https://{self.host}", host=self.host),
            ws_open(self.port, cookie, origin=f"https://{self.host}", host=self.host),
        )
        statuses = sorted([first[0], second[0]])
        for result in (first, second):
            result[3].close()
        self.assertEqual(statuses, [101, 409])

    async def test_a_new_viewer_can_connect_after_the_first_leaves(self):
        await self.start()
        cookie = await self.redeem()
        status, _, _, writer = await ws_open(self.port, cookie, origin=f"https://{self.host}", host=self.host)
        self.assertEqual(status, 101)
        writer.close()
        for _ in range(50):
            await asyncio.sleep(0.05)
            status, _, _, writer = await ws_open(self.port, cookie, origin=f"https://{self.host}", host=self.host)
            writer.close()
            if status == 101:
                break
        self.assertEqual(status, 101)


class SessionEndTests(RemoteE2ETests):

    async def test_stop_ends_all_access(self):
        harness, session = await self.start()
        cookie = await self.redeem()
        status, _, _, writer = await ws_open(self.port, cookie, origin=f"https://{self.host}", host=self.host)
        self.assertEqual(status, 101)
        await session.stop("requested")
        writer.close()
        self.assertEqual(session.state, "stopped")
        self.assertNotEqual(await self.try_http("GET", "/", headers={"Cookie": f"{COOKIE_NAME}={cookie}"}), 200)
        self.assertNotEqual(await self.try_http("GET", f"/connect/{self.secret}"), 303)
        try:
            status, _, _, w2 = await ws_open(self.port, cookie, origin=f"https://{self.host}", host=self.host)
            w2.close()
            self.assertNotEqual(status, 101)
        except OSError:
            pass  # listener already closed: the viewer cannot connect at all

    async def test_time_limit_stops_the_session(self):
        harness, session = await self.start(duration=0.3)
        await asyncio.wait_for(session.wait_closed(), 5)
        self.assertEqual(session.state, "stopped")
        self.assertEqual(session.reason, "timeout")
        self.assertIn("time limit reached", " ".join(harness.messages))
        self.assertNotEqual(await self.try_http("GET", f"/connect/{self.secret}"), 303)

    async def test_input_is_rejected_after_stop(self):
        harness, session = await self.start()
        cookie = await self.redeem()
        status, _, _, writer = await ws_open(self.port, cookie, origin=f"https://{self.host}", host=self.host)
        self.assertEqual(status, 101)
        writer.write(client_frame(OP_TEXT, b'{"k":"move","x":0.5,"y":0.5}'))
        await writer.drain()
        for _ in range(100):
            if harness.backend.calls:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(harness.backend.calls, "input must reach the backend while the session is active")

        await session.stop("requested")
        before = list(harness.backend.calls)
        try:
            writer.write(client_frame(OP_TEXT, b'{"k":"move","x":0.1,"y":0.1}'))
            await writer.drain()
        except OSError:
            pass  # the server already closed the socket, which is also a refusal
        writer.close()
        await asyncio.sleep(0.1)
        self.assertEqual(harness.backend.calls, before, "no input may reach the PC after stop")


if __name__ == "__main__":
    unittest.main()
