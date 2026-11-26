"""Shared loopback HTTP / WebSocket client for the remote-session tests (not a test module)."""

from __future__ import annotations

import asyncio
import base64
import os
import struct


async def close_writer(writer) -> None:
    """Close a client stream and wait until it is really closed.

    On Windows the proactor loop can report the peer's reset here; that is not a test
    failure, so OSError (ConnectionError is a subclass) and a slow close are ignored.
    """
    try:
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), 2)
    except (OSError, asyncio.TimeoutError):
        pass


async def http_request(port: int, method: str, path: str, headers: dict[str, str] | None = None,
                       raw_body: bytes | None = None, host: str = "viewer.example"
                       ) -> tuple[int, dict[str, str], bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    lines = [f"{method} {path} HTTP/1.1", f"Host: {host}", "Connection: close"]
    for name, value in (headers or {}).items():
        lines.append(f"{name}: {value}")
    request = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
    writer.write(request + (raw_body or b""))
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), 10)
    await close_writer(writer)
    head, _, body = data.partition(b"\r\n\r\n")
    status_line, *header_lines = head.decode("latin-1").split("\r\n")
    status = int(status_line.split(" ")[1])
    parsed = {}
    for line in header_lines:
        name, _, value = line.partition(":")
        parsed[name.strip().lower()] = value.strip()
    return status, parsed, body


async def ws_open(port: int, cookie: str | None, origin: str | None = None,
                  host: str = "viewer.example"):
    """Open a WebSocket. Returns (status, headers, reader, writer)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    lines = ["GET /ws HTTP/1.1", f"Host: {host}", "Upgrade: websocket", "Connection: Upgrade",
             f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
    if cookie is not None:
        lines.append(f"Cookie: pcr_session={cookie}")
    if origin is not None:
        lines.append(f"Origin: {origin}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
    status_line, *header_lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in header_lines:
        name, _, value = line.partition(":")
        if name:
            headers[name.strip().lower()] = value.strip()
    headers["_key"] = key
    return int(status_line.split(" ")[1]), headers, reader, writer


def client_frame(opcode: int, payload: bytes = b"", masked: bool = True) -> bytes:
    head = bytes([0x80 | opcode])
    size = len(payload)
    mask_bit = 0x80 if masked else 0
    if size < 126:
        head += bytes([mask_bit | size])
    elif size < 65536:
        head += bytes([mask_bit | 126]) + struct.pack("!H", size)
    else:
        head += bytes([mask_bit | 127]) + struct.pack("!Q", size)
    if not masked:
        return head + payload
    mask = os.urandom(4)
    masked_payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return head + mask + masked_payload


async def read_server_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    first, second = await reader.readexactly(2)
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
    return opcode, await reader.readexactly(length)


async def next_json(reader: asyncio.StreamReader, wanted: str, timeout: float = 5.0) -> dict:
    """Read frames until a JSON text frame with ``t == wanted`` arrives."""
    import json

    async def scan():
        while True:
            opcode, payload = await read_server_frame(reader)
            if opcode == 0x1:
                message = json.loads(payload.decode("utf-8"))
                if message.get("t") == wanted:
                    return message

    return await asyncio.wait_for(scan(), timeout)


async def next_binary(reader: asyncio.StreamReader, timeout: float = 5.0) -> bytes:
    async def scan():
        while True:
            opcode, payload = await read_server_frame(reader)
            if opcode == 0x2:
                return payload

    return await asyncio.wait_for(scan(), timeout)
