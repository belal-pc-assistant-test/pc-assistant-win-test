"""Minimal HTTP/1.1 request parsing and RFC 6455 WebSocket framing (standard library only).

Deliberately strict: duplicate headers are refused, client frames must be masked,
fragmented messages and reserved bits are refused, and inbound payloads are capped.
"""

from __future__ import annotations

import base64
import hashlib
import struct
from typing import NamedTuple

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x1, 0x2, 0x8, 0x9, 0xA
MAX_HEAD_BYTES = 16 * 1024
MAX_INBOUND_PAYLOAD = 64 * 1024


class ProtocolError(ValueError):
    """The peer sent something we refuse to process."""


class Request(NamedTuple):
    method: str
    target: str
    version: str
    headers: dict[str, str]


def parse_head(raw: bytes) -> Request:
    """Parse the request line and headers (everything up to the blank line)."""
    if len(raw) > MAX_HEAD_BYTES:
        raise ProtocolError("request head too large")
    try:
        text = raw.decode("latin-1")
    except UnicodeDecodeError as exc:  # latin-1 cannot fail; defensive
        raise ProtocolError("bad encoding") from exc
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise ProtocolError("bad request line")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep or not name or name != name.strip():
            raise ProtocolError("bad header line")
        key = name.lower()
        if key in headers:
            raise ProtocolError("duplicate header")
        headers[key] = value.strip()
    return Request(parts[0], parts[1], parts[2], headers)


def parse_cookies(header: str | None) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in (header or "").split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name and name not in cookies:
            cookies[name] = value
    return cookies


def accept_value(key: str) -> str:
    digest = hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def valid_ws_key(key: str) -> bool:
    try:
        return len(base64.b64decode(key, validate=True)) == 16
    except (ValueError, TypeError):
        return False


def http_response(status: int, reason: str, headers: list[tuple[str, str]] | None = None,
                  body: bytes = b"") -> bytes:
    lines = [f"HTTP/1.1 {status} {reason}",
             f"Content-Length: {len(body)}",
             "Connection: close",
             "Cache-Control: no-store",
             "Referrer-Policy: no-referrer",
             "X-Content-Type-Options: nosniff",
             "X-Frame-Options: DENY"]
    for name, value in headers or []:
        lines.append(f"{name}: {value}")
    head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
    return head + body


def websocket_handshake(key: str) -> bytes:
    return ("HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept_value(key)}\r\n"
            "Cache-Control: no-store\r\n\r\n").encode("latin-1")


def ws_frame(opcode: int, payload: bytes = b"") -> bytes:
    """Encode one unmasked, final frame (server to client)."""
    size = len(payload)
    head = bytes([0x80 | opcode])
    if size < 126:
        head += bytes([size])
    elif size < 65536:
        head += bytes([126]) + struct.pack("!H", size)
    else:
        head += bytes([127]) + struct.pack("!Q", size)
    return head + payload


def _unmask(data: bytes, mask: bytes) -> bytes:
    size = len(data)
    if size == 0:
        return b""
    repeated = (mask * (size // 4 + 1))[:size]
    value = int.from_bytes(data, "big") ^ int.from_bytes(repeated, "big")
    return value.to_bytes(size, "big")


async def read_ws_frame(reader) -> tuple[int, bytes]:
    """Read one client frame. Raises ProtocolError for anything we do not accept."""
    first, second = await reader.readexactly(2)
    if not first & 0x80:
        raise ProtocolError("fragmented messages are not supported")
    if first & 0x70:
        raise ProtocolError("reserved bits set")
    opcode = first & 0x0F
    if opcode not in (OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG):
        raise ProtocolError("unsupported opcode")
    if not second & 0x80:
        raise ProtocolError("client frames must be masked")
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
    if length > MAX_INBOUND_PAYLOAD:
        raise ProtocolError("frame too large")
    if opcode >= 0x8 and length > 125:
        raise ProtocolError("control frame too large")
    mask = await reader.readexactly(4)
    payload = _unmask(await reader.readexactly(length), mask)
    return opcode, payload
