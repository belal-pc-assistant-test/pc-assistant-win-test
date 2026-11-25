"""HTTP head parsing and the RFC 6455 framing used by the remote viewer."""

from __future__ import annotations

import asyncio
import struct
import unittest

from _bootstrap import PROJECT_DIR
assert PROJECT_DIR.is_dir()
from assistant.remote import wire
from assistant.remote.wire import ProtocolError


def reader_with(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


def masked_client_frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    head = bytes([(0x80 if fin else 0) | opcode])
    mask = b"\x01\x02\x03\x04"
    size = len(payload)
    if size < 126:
        head += bytes([0x80 | size])
    elif size < 65536:
        head += bytes([0x80 | 126]) + struct.pack("!H", size)
    else:
        head += bytes([0x80 | 127]) + struct.pack("!Q", size)
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


class HeadParsingTests(unittest.TestCase):
    def test_parses_request_line_and_lowercases_headers(self):
        request = wire.parse_head(b"GET /ws?x=1 HTTP/1.1\r\nHost: a.example\r\nUpgrade: websocket\r\n\r\n")
        self.assertEqual((request.method, request.target), ("GET", "/ws?x=1"))
        self.assertEqual(request.headers["host"], "a.example")
        self.assertEqual(request.headers["upgrade"], "websocket")

    def test_duplicate_header_is_refused(self):
        with self.assertRaises(ProtocolError):
            wire.parse_head(b"GET / HTTP/1.1\r\nHost: a\r\nHost: b\r\n\r\n")

    def test_bad_request_line_and_bad_header_are_refused(self):
        with self.assertRaises(ProtocolError):
            wire.parse_head(b"NOT HTTP\r\n\r\n")
        with self.assertRaises(ProtocolError):
            wire.parse_head(b"GET / HTTP/1.1\r\nno-colon-here\r\n\r\n")

    def test_oversized_head_is_refused(self):
        with self.assertRaises(ProtocolError):
            wire.parse_head(b"GET / HTTP/1.1\r\n" + b"X: y\r\n" * 5000 + b"\r\n")

    def test_cookie_parsing_takes_the_first_value(self):
        cookies = wire.parse_cookies("a=1; pcr_session=abc; pcr_session=evil")
        self.assertEqual(cookies["pcr_session"], "abc")
        self.assertEqual(wire.parse_cookies(None), {})


class HandshakeTests(unittest.TestCase):
    def test_accept_value_matches_rfc6455_example(self):
        self.assertEqual(wire.accept_value("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_key_must_decode_to_16_bytes(self):
        self.assertTrue(wire.valid_ws_key("dGhlIHNhbXBsZSBub25jZQ=="))
        self.assertFalse(wire.valid_ws_key("c2hvcnQ="))
        self.assertFalse(wire.valid_ws_key("not base64!!"))

    def test_http_responses_carry_security_headers(self):
        raw = wire.http_response(403, "Forbidden", [("Retry-After", "60")], b"Forbidden")
        self.assertIn(b"HTTP/1.1 403 Forbidden", raw)
        self.assertIn(b"Cache-Control: no-store", raw)
        self.assertIn(b"Referrer-Policy: no-referrer", raw)
        self.assertIn(b"X-Frame-Options: DENY", raw)
        self.assertIn(b"Content-Length: 9", raw)


class FramingTests(unittest.TestCase):
    def test_server_frames_have_correct_length_encoding(self):
        self.assertEqual(wire.ws_frame(wire.OP_TEXT, b"hi"), b"\x81\x02hi")
        medium = wire.ws_frame(wire.OP_BINARY, b"x" * 300)
        self.assertEqual(medium[1], 126)
        self.assertEqual(struct.unpack("!H", medium[2:4])[0], 300)
        large = wire.ws_frame(wire.OP_BINARY, b"x" * 70000)
        self.assertEqual(large[1], 127)
        self.assertEqual(struct.unpack("!Q", large[2:10])[0], 70000)

    def test_masked_client_frames_round_trip(self):
        async def run():
            for size in (0, 5, 125, 126, 1000, 65535):
                payload = bytes(range(256)) * (size // 256) + bytes(size % 256)
                opcode, decoded = await wire.read_ws_frame(
                    reader_with(masked_client_frame(wire.OP_TEXT, payload)))
                self.assertEqual((opcode, decoded), (wire.OP_TEXT, payload))
        asyncio.run(run())

    def test_unmasked_client_frame_is_refused(self):
        async def run():
            raw = b"\x81\x02hi"  # no mask bit
            with self.assertRaises(ProtocolError):
                await wire.read_ws_frame(reader_with(raw))
        asyncio.run(run())

    def test_fragmented_reserved_and_unknown_opcodes_are_refused(self):
        async def run():
            with self.assertRaises(ProtocolError):
                await wire.read_ws_frame(reader_with(masked_client_frame(wire.OP_TEXT, b"a", fin=False)))
            with self.assertRaises(ProtocolError):
                await wire.read_ws_frame(reader_with(bytes([0xC1, 0x80]) + b"\0\0\0\0"))  # RSV1 set
            with self.assertRaises(ProtocolError):
                await wire.read_ws_frame(reader_with(masked_client_frame(0x3, b"")))  # reserved opcode
        asyncio.run(run())

    def test_oversized_inbound_frame_is_refused_before_reading_it(self):
        async def run():
            header = bytes([0x81, 0x80 | 127]) + struct.pack("!Q", wire.MAX_INBOUND_PAYLOAD + 1) + b"\0\0\0\0"
            with self.assertRaises(ProtocolError):
                await wire.read_ws_frame(reader_with(header))
        asyncio.run(run())

    def test_oversized_control_frame_is_refused(self):
        async def run():
            with self.assertRaises(ProtocolError):
                await wire.read_ws_frame(reader_with(masked_client_frame(wire.OP_PING, b"x" * 126)))
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
