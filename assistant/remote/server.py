"""Loopback HTTP + WebSocket server for the remote viewer.

Bound to 127.0.0.1 only; the public address is provided by the tunnel process.

Routes
    GET /connect/<secret>   redeem the one-time link: sets the session cookie, 303 to /
    GET /                   viewer page (cookie required)
    GET /ws                 WebSocket: JPEG frames down, JSON pings and input up (cookie required)

Every request without a valid cookie (or, for /connect/, a valid unused secret) gets
403, including unknown paths. Rate-limited requests also get 403 plus Retry-After, so
the response never reveals why access was refused.
"""

from __future__ import annotations

import asyncio
import json
import struct
import time
import urllib.parse
from typing import Any, Callable

from .gate import AccessGate
from .stream import FrameBroker
from .viewer_page import VIEWER_HTML
from .wire import (OP_BINARY, OP_CLOSE, OP_PING, OP_PONG, OP_TEXT, ProtocolError, http_response,
                   parse_cookies, parse_head, read_ws_frame, valid_ws_key, websocket_handshake, ws_frame)

COOKIE_NAME = "pcr_session"
HEAD_TIMEOUT = 10.0


def host_key(value: str) -> str:
    """Canonical form of a host name for comparisons.

    Lower case, with a default port (``:80`` or ``:443``) removed, so that
    ``ABC.trycloudflare.com:443`` and ``abc.trycloudflare.com`` are the same host.
    Used for both the allowed names and every incoming Host or Origin.
    """
    text = (value or "").strip().lower()
    host, sep, port = text.rpartition(":")
    if sep and port in ("80", "443"):
        return host
    return text
STATS_INTERVAL = 1.0
MAX_CONNECTIONS = 32
PAGE_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "img-src 'self' blob: data:; connect-src 'self' ws: wss:; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'none'")


class RemoteServer:
    def __init__(self, *, gate: AccessGate, broker: FrameBroker,
                 on_input: Callable[[Any], None],
                 on_viewer: Callable[[bool], None],
                 on_sent: Callable[[float], None],
                 stats: Callable[[], dict],
                 on_denied: Callable[[str], None] | None = None,
                 allowed_hosts: set[str] | None = None) -> None:
        self.gate = gate
        self.broker = broker
        self.on_input = on_input
        self.on_viewer = on_viewer
        self.on_sent = on_sent
        self.stats = stats
        self.on_denied = on_denied or (lambda decision: None)
        self._allowed_hosts: set[str] = set()
        self.allowed_hosts = allowed_hosts or set()
        self.sent = 0
        self.dropped = 0
        self._server: asyncio.base_events.Server | None = None
        self._connections: set[asyncio.StreamWriter] = set()
        self._viewer: asyncio.StreamWriter | None = None
        self.port = 0

    @property
    def viewer_connected(self) -> bool:
        return self._viewer is not None

    @property
    def allowed_hosts(self) -> set[str]:
        """Public host names (the tunnel address), normalized with :func:`host_key`."""
        return self._allowed_hosts

    @allowed_hosts.setter
    def allowed_hosts(self, hosts) -> None:
        self._allowed_hosts = {host_key(h) for h in (hosts or set()) if host_key(h)}

    def _loopback_names(self) -> set[str]:
        return {host_key(f"127.0.0.1:{self.port}"), host_key(f"localhost:{self.port}"),
                "127.0.0.1", "localhost"}

    def host_ok(self, host_header: str | None) -> bool:
        """Host must be the tunnel's public name or this machine's loopback name."""
        key = host_key(host_header or "")
        return bool(key) and (key in self.allowed_hosts or key in self._loopback_names())

    def origin_ok(self, origin: str, host_header: str | None) -> bool:
        """Origin must be the public tunnel name, or the same loopback host as Host."""
        key = host_key(urllib.parse.urlsplit(origin).netloc)
        if not key:
            return False  # "null" and other opaque origins are refused
        return key in self.allowed_hosts or key == host_key(host_header or "")

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._client, host="127.0.0.1", port=0,
                                                  limit=16 * 1024)
        self.port = int(self._server.sockets[0].getsockname()[1])
        return self.port

    async def close(self) -> None:
        viewer = self._viewer
        if viewer is not None:
            try:
                viewer.write(ws_frame(OP_CLOSE, struct.pack("!H", 1000) + b"session ended"))
            except (OSError, RuntimeError):
                pass
        if self._server is not None:
            self._server.close()
        for writer in list(self._connections):
            try:
                writer.close()
            except (OSError, RuntimeError):
                pass
        self._connections.clear()
        self._viewer = None
        if self._server is not None:
            try:
                await asyncio.wait_for(self._server.wait_closed(), 2)
            except (asyncio.TimeoutError, OSError):
                pass

    # ------------------------------------------------------------------
    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if len(self._connections) >= MAX_CONNECTIONS:
            writer.close()
            return
        self._connections.add(writer)
        try:
            await self._serve(reader, writer)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError,
                ConnectionError, ProtocolError, ValueError, OSError):
            pass
        finally:
            self._connections.discard(writer)
            try:
                writer.close()
            except (OSError, RuntimeError):
                pass

    async def _respond(self, writer: asyncio.StreamWriter, status: int, reason: str,
                       headers: list[tuple[str, str]] | None = None, body: bytes = b"") -> None:
        writer.write(http_response(status, reason, headers, body))
        await writer.drain()

    async def _deny(self, writer: asyncio.StreamWriter, decision: str) -> None:
        if decision not in ("missing",):
            self.on_denied(decision)
        headers = [("Content-Type", "text/plain; charset=utf-8")]
        if decision == "limited":
            headers.append(("Retry-After", "60"))
        await self._respond(writer, 403, "Forbidden", headers, b"Forbidden")

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
        try:
            request = parse_head(raw)
        except ProtocolError:
            await self._respond(writer, 400, "Bad Request", [("Content-Type", "text/plain")], b"Bad request")
            return
        path = request.target.split("?", 1)[0]

        # DNS rebinding and cross-site requests: the Host and Origin must name
        # this tunnel or this machine. Checked before any route or secret use.
        if not self.host_ok(request.headers.get("host")):
            await self._deny(writer, "host")
            return
        origin = request.headers.get("origin")
        if origin is not None and not self.origin_ok(origin, request.headers.get("host")):
            await self._deny(writer, "origin")
            return

        if path.startswith("/connect/"):
            if request.method != "GET":
                await self._deny(writer, "denied")
                return
            secret = urllib.parse.unquote(path[len("/connect/"):])
            decision, cookie = self.gate.redeem(secret)
            if decision != "ok":
                await self._deny(writer, decision)
                return
            cookie_header = (f"{COOKIE_NAME}={cookie}; Path=/; HttpOnly; Secure; SameSite=Strict; "
                             "Max-Age=7200")
            await self._respond(writer, 303, "See Other",
                                [("Location", "/"), ("Set-Cookie", cookie_header)])
            return

        cookie = parse_cookies(request.headers.get("cookie")).get(COOKIE_NAME)
        decision = self.gate.authorize(cookie)
        if decision != "ok":
            await self._deny(writer, decision)
            return
        if request.method != "GET":
            await self._respond(writer, 405, "Method Not Allowed", [("Allow", "GET")], b"")
            return
        if path == "/ws" and request.headers.get("upgrade", "").lower() == "websocket":
            await self._websocket(request, reader, writer)
            return
        if path in ("/", "/index.html"):
            body = VIEWER_HTML.encode("utf-8")
            await self._respond(writer, 200, "OK", [
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Security-Policy", PAGE_CSP),
            ], body)
            return
        await self._respond(writer, 404, "Not Found", [("Content-Type", "text/plain")], b"Not found")

    async def _websocket(self, request, reader, writer) -> None:
        if self._viewer is not None:
            await self._respond(writer, 409, "Conflict", [], b"")
            return
        origin = request.headers.get("origin")
        if origin is not None and not self.origin_ok(origin, request.headers.get("host")):
            await self._deny(writer, "origin")
            return
        key = request.headers.get("sec-websocket-key", "")
        if not valid_ws_key(key) or request.headers.get("sec-websocket-version") != "13":
            await self._respond(writer, 400, "Bad Request", [], b"")
            return

        writer.write(websocket_handshake(key))
        await writer.drain()
        self._viewer = writer
        self.on_viewer(True)
        self._send_text(writer, {"t": "hello", **self._safe_stats()})
        sender = asyncio.create_task(self._send_frames(writer))
        stats = asyncio.create_task(self._send_stats(writer))
        try:
            while True:
                opcode, payload = await read_ws_frame(reader)
                if opcode == OP_CLOSE:
                    break
                if opcode == OP_PING:
                    writer.write(ws_frame(OP_PONG, payload))
                elif opcode == OP_TEXT:
                    self._on_text(writer, payload)
                elif opcode == OP_BINARY:
                    continue
                elif opcode == OP_PONG:
                    continue
        finally:
            sender.cancel()
            stats.cancel()
            if self._viewer is writer:
                self._viewer = None
                self.on_viewer(False)
            try:
                writer.write(ws_frame(OP_CLOSE, struct.pack("!H", 1000)))
            except (OSError, RuntimeError):
                pass

    def _on_text(self, writer: asyncio.StreamWriter, payload: bytes) -> None:
        try:
            message = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return
        if not isinstance(message, dict):
            return
        if message.get("t") == "ping":
            ident = message.get("id")
            if not isinstance(ident, (int, float)) or isinstance(ident, bool):
                ident = None
            self._send_text(writer, {"t": "pong", "id": ident})
            return
        self.on_input(message)

    def _send_text(self, writer: asyncio.StreamWriter, obj: dict) -> None:
        writer.write(ws_frame(OP_TEXT, json.dumps(obj, separators=(",", ":")).encode("utf-8")))

    def _safe_stats(self) -> dict:
        data = dict(self.stats())
        data.update({"sent": self.sent, "dropped": self.dropped})
        return data

    async def _send_frames(self, writer: asyncio.StreamWriter) -> None:
        last = 0
        while True:
            seq, frame, skipped = await self.broker.wait_newer(last)
            last = seq
            started = time.monotonic()
            writer.write(ws_frame(OP_BINARY, frame.jpeg))
            await writer.drain()
            self.on_sent(time.monotonic() - started)
            self.sent += 1
            self.dropped += skipped

    async def _send_stats(self, writer: asyncio.StreamWriter) -> None:
        while True:
            await asyncio.sleep(STATS_INTERVAL)
            self._send_text(writer, {"t": "stats", **self._safe_stats()})
            await writer.drain()
