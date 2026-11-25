"""Loopback HTTP/WebSocket server: gating, single viewer, frames, input delivery, failure limits."""

from __future__ import annotations

import asyncio
import json
import unittest

from _bootstrap import PROJECT_DIR
from _remote_helpers import client_frame, http_request, next_binary, next_json, ws_open
from assistant.remote.gate import FAILURE_LIMIT, AccessGate, generate_secret
from assistant.remote.server import COOKIE_NAME, RemoteServer
from assistant.remote.stream import FrameBroker, JpegFrame

assert PROJECT_DIR.is_dir()
JPEG = b"\xff\xd8\xff\xe0test-frame\xff\xd9"
HOST = "fakeone-test.trycloudflare.com"


class ServerHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.clock = [1000.0]
        self.secret = generate_secret()
        self.gate = AccessGate(self.secret, deadline=self.clock[0] + 600, now=lambda: self.clock[0])
        self.broker = FrameBroker()
        self.inputs: list = []
        self.viewer_events: list[bool] = []
        self.denied: list[str] = []
        self.server = RemoteServer(
            gate=self.gate, broker=self.broker,
            on_input=self.inputs.append,
            on_viewer=self.viewer_events.append,
            on_sent=lambda seconds: None,
            stats=lambda: {"remaining": 500, "quality": 60, "fps_target": 8, "inputs": 0, "viewers": 0},
            on_denied=self.denied.append,
            allowed_hosts={HOST},
        )
        self.port = await self.server.start()
        self.addAsyncCleanup(self.server.close)

    async def redeem(self) -> str:
        status, headers, _ = await http_request(self.port, "GET", f"/connect/{self.secret}", host=HOST)
        self.assertEqual(status, 303)
        return headers["set-cookie"].split(f"{COOKIE_NAME}=")[1].split(";")[0]


class ServerBindingTests(ServerHarness):
    async def test_listens_on_loopback_only(self):
        address = self.server._server.sockets[0].getsockname()
        self.assertEqual(address[0], "127.0.0.1")

    async def test_unknown_paths_and_missing_cookie_get_403(self):
        self.assertEqual((await http_request(self.port, "GET", "/", host=HOST))[0], 403)
        self.assertEqual((await http_request(self.port, "GET", "/other", host=HOST))[0], 403)
        self.assertEqual((await http_request(self.port, "GET", "/ws", host=HOST))[0], 403)

    async def test_wrong_method_on_connect_is_refused(self):
        status, _, _ = await http_request(self.port, "POST", f"/connect/{self.secret}", host=HOST)
        self.assertEqual(status, 403)
        self.assertEqual(self.gate.state, "fresh", "a wrong method must not consume the secret")

    async def test_garbage_request_gets_400_not_a_crash(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(b"NOT HTTP AT ALL\r\n\r\n")
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), 5)
        writer.close()
        self.assertTrue(data.startswith(b"HTTP/1.1 400"))


class ViewerTests(ServerHarness):
    async def test_redeem_then_page_and_security_headers(self):
        cookie = await self.redeem()
        status, headers, body = await http_request(
            self.port, "GET", "/", headers={"Cookie": f"{COOKIE_NAME}={cookie}"}, host=HOST)
        self.assertEqual(status, 200)
        self.assertIn("default-src 'none'", headers["content-security-policy"])
        self.assertNotIn(b"http://", body.replace(b"http://www.w3.org", b""))  # no external resources
        self.assertNotIn(b"<script src", body)

    async def test_websocket_receives_hello_and_frames_and_sends_input(self):
        cookie = await self.redeem()
        status, headers, reader, writer = await ws_open(self.port, cookie, origin=f"https://{HOST}", host=HOST)
        self.assertEqual(status, 101)
        self.assertEqual(headers["sec-websocket-accept"] != "", True)
        hello = await next_json(reader, "hello")
        self.assertEqual(hello["fps_target"], 8)
        self.assertNotIn("secret", json.dumps(hello).lower())
        self.assertEqual(self.viewer_events, [True])

        self.broker.publish(JpegFrame(JPEG, 1280, 720))
        self.assertEqual(await next_binary(reader), JPEG)

        writer.write(client_frame(0x1, json.dumps({"k": "move", "x": 0.25, "y": 0.75}).encode()))
        await writer.drain()
        for _ in range(100):
            if self.inputs:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.inputs, [{"k": "move", "x": 0.25, "y": 0.75}])

        writer.write(client_frame(0x1, json.dumps({"t": "ping", "id": 7}).encode()))
        await writer.drain()
        pong = await next_json(reader, "pong")
        self.assertEqual(pong["id"], 7)
        writer.close()
        for _ in range(100):
            if self.viewer_events[-1] is False:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.viewer_events, [True, False])

    async def test_second_viewer_is_refused_while_one_is_connected(self):
        cookie = await self.redeem()
        status, _, reader, writer = await ws_open(self.port, cookie, origin=f"https://{HOST}", host=HOST)
        self.assertEqual(status, 101)
        second, _, _, writer2 = await ws_open(self.port, cookie, origin=f"https://{HOST}", host=HOST)
        self.assertEqual(second, 409)
        writer2.close()
        writer.close()

    async def test_foreign_origin_is_refused(self):
        cookie = await self.redeem()
        status, _, _, writer = await ws_open(self.port, cookie, origin="https://evil.example", host=HOST)
        self.assertEqual(status, 403)
        self.assertIn("origin", self.denied)
        writer.close()

    async def test_malformed_input_is_ignored_and_does_not_reach_control(self):
        cookie = await self.redeem()
        status, _, reader, writer = await ws_open(self.port, cookie, origin=f"https://{HOST}", host=HOST)
        self.assertEqual(status, 101)
        writer.write(client_frame(0x1, b"not json"))
        writer.write(client_frame(0x1, b"[1,2,3]"))
        await writer.drain()
        await asyncio.sleep(0.05)
        self.assertEqual(self.inputs, [])
        writer.close()

    async def test_server_close_disconnects_an_active_viewer(self):
        cookie = await self.redeem()
        status, _, reader, writer = await ws_open(self.port, cookie, origin=f"https://{HOST}", host=HOST)
        await next_json(reader, "hello")
        await self.server.close()
        data = await asyncio.wait_for(reader.read(), 5)
        self.assertTrue(data == b"" or data.startswith(b"\x88"))
        writer.close()


class FailureLimitTests(ServerHarness):
    async def test_repeated_wrong_secrets_lock_out_even_the_right_one(self):
        for _ in range(FAILURE_LIMIT):
            status, _, _ = await http_request(self.port, "GET", "/connect/" + "x" * 43, host=HOST)
            self.assertEqual(status, 403)
        status, headers, _ = await http_request(self.port, "GET", f"/connect/{self.secret}", host=HOST)
        self.assertEqual(status, 403)
        self.assertEqual(headers.get("retry-after"), "60")
        self.assertIn("denied", self.denied)

    async def test_correct_secret_works_only_once(self):
        cookie = await self.redeem()
        self.assertTrue(cookie)
        status, _, _ = await http_request(self.port, "GET", f"/connect/{self.secret}", host=HOST)
        self.assertEqual(status, 403)

    async def test_expired_session_refuses_the_cookie_too(self):
        cookie = await self.redeem()
        self.clock[0] += 601
        status, _, _ = await http_request(self.port, "GET", "/", headers={"Cookie": f"{COOKIE_NAME}={cookie}"}, host=HOST)
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
