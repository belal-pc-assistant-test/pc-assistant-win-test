"""Outbound redaction: every message and upload the bot sends to Telegram (Round 7).

These tests drive a real ``telegram.Bot`` through the wrapper in
``assistant/outbound.py``. Only the network edge is replaced: a recording request
object, or a real loopback HTTP server for the on-the-wire test.
"""

from __future__ import annotations

import http.server
import io
import json
import os
import threading
import unittest
from unittest import mock
from urllib.parse import unquote_plus

from _bootstrap import PROJECT_DIR
assert PROJECT_DIR.is_dir()

from telegram import Bot, InputFile  # noqa: E402
from telegram.request import BaseRequest, HTTPXRequest, RequestData  # noqa: E402

from assistant import audit, outbound  # noqa: E402
from assistant.outbound import (RedactingHTTPXRequest, RedactingRequestMixin,  # noqa: E402
                                SecretInFileError, prepare_request_data, redact_value)

TOKEN = "987654321:AAHround7TestToken_abcdefghijklmnop"
MESSAGE = {"message_id": 7, "date": 1700000000, "chat": {"id": 1, "type": "private"}}


class _RecordingTransport(BaseRequest):
    """Stands in for the network: records what would have been sent."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[RequestData | None] = []

    @property
    def read_timeout(self):
        return None

    async def initialize(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def do_request(self, url, method, request_data=None, **kwargs):
        self.calls.append(request_data)
        return 200, json.dumps({"ok": True, "result": MESSAGE}).encode("utf-8")


class _Wrapped(RedactingRequestMixin, _RecordingTransport):
    """The production arrangement: the wrapper in front of the transport."""


class OutboundRedactionTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self) -> None:
        audit.register_secret(TOKEN)
        self.transport = _Wrapped()
        self.bot = Bot(TOKEN, request=self.transport)

    def sent_parameters(self) -> dict:
        self.assertTrue(self.transport.calls, "nothing reached the transport")
        return self.transport.calls[-1].parameters

    def sent_json(self) -> str:
        self.assertTrue(self.transport.calls, "nothing reached the transport")
        return json.dumps(self.transport.calls[-1].json_parameters, ensure_ascii=False)

    async def test_reply_text_is_redacted(self):
        await self.bot.send_message(chat_id=1, text=f"plan for {TOKEN} ready")
        self.assertEqual(self.sent_parameters()["text"], "plan for [REDACTED] ready")

    async def test_error_text_built_from_an_exception_is_redacted(self):
        exc = RuntimeError(f"cloud planner failed: key={TOKEN}")
        await self.bot.send_message(chat_id=1, text=f"Error: {exc}")
        self.assertNotIn(TOKEN, self.sent_json())
        self.assertIn("[REDACTED]", self.sent_parameters()["text"])

    async def test_edited_message_text_is_redacted(self):
        await self.bot.edit_message_text(text=f"working on {TOKEN}", chat_id=1, message_id=7)
        self.assertEqual(self.sent_parameters()["text"], "working on [REDACTED]")

    async def test_caption_is_redacted(self):
        await self.bot.send_document(chat_id=1, document=InputFile(b"report", filename="r.txt"),
                                     caption=f"caption with {TOKEN}")
        self.assertEqual(self.sent_parameters()["caption"], "caption with [REDACTED]")
        self.assertTrue(self.transport.calls[-1].contains_files)

    async def test_keyboard_label_inside_nested_markup_is_redacted(self):
        markup = {"inline_keyboard": [[{"text": TOKEN, "callback_data": "x"}]]}
        await self.bot.send_message(chat_id=1, text="pick", reply_markup=markup)
        self.assertNotIn(TOKEN, self.sent_json())
        self.assertIn("[REDACTED]", self.sent_json())

    async def test_text_without_a_secret_is_sent_unchanged(self):
        await self.bot.send_message(chat_id=1, text="hello there, نص عربي")
        self.assertEqual(self.sent_parameters()["text"], "hello there, نص عربي")

    async def test_file_whose_bytes_contain_the_token_is_refused(self):
        data = b"header " + TOKEN.encode("utf-8") + b" footer"
        with self.assertRaises(SecretInFileError):
            await self.bot.send_document(chat_id=1, document=InputFile(data, filename="notes.txt"))
        self.assertEqual(self.transport.calls, [], "the refused upload must not reach the network")

    async def test_file_name_containing_the_token_is_refused(self):
        with self.assertRaises(SecretInFileError):
            await self.bot.send_document(chat_id=1, document=InputFile(b"ok", filename=f"{TOKEN}.txt"))
        self.assertEqual(self.transport.calls, [])

    async def test_token_split_across_scan_steps_is_still_found(self):
        # Default InputFile reads a file handle into bytes (the bot's normal path).
        data = b"x" * 10 + TOKEN.encode("utf-8") + b"y" * 10
        with mock.patch.object(outbound, "SCAN_CHUNK", 16):
            with self.assertRaises(SecretInFileError):
                await self.bot.send_document(chat_id=1, document=InputFile(io.BytesIO(data), filename="a.bin"))
        self.assertEqual(self.transport.calls, [])

    async def test_token_split_across_scan_steps_is_found_in_a_streamed_upload(self):
        # read_file_handle=False keeps the handle, so the scan runs on the stream itself.
        data = b"x" * 10 + TOKEN.encode("utf-8") + b"y" * 10
        with mock.patch.object(outbound, "SCAN_CHUNK", 16):
            with self.assertRaises(SecretInFileError):
                await self.bot.send_document(
                    chat_id=1, document=InputFile(io.BytesIO(data), filename="a.bin", read_file_handle=False))
        self.assertEqual(self.transport.calls, [])

    async def test_clean_streamed_upload_is_sent_and_its_position_is_restored(self):
        stream = io.BytesIO(b"hello world")
        await self.bot.send_document(chat_id=1, document=InputFile(stream, filename="ok.txt",
                                                                   read_file_handle=False))
        self.assertEqual(stream.tell(), 0, "the scan must leave the stream where it was")
        self.assertEqual(len(self.transport.calls), 1)

    async def test_streamed_upload_that_cannot_be_inspected_is_refused(self):
        class ReadOnly:  # no tell() or seek(): the scan cannot restore its position
            def read(self, size: int = -1) -> bytes:
                return b""

        with self.assertRaises(SecretInFileError):
            await self.bot.send_document(chat_id=1, document=InputFile(ReadOnly(), filename="x.bin",
                                                                       read_file_handle=False))
        self.assertEqual(self.transport.calls, [])

    async def test_redact_value_covers_nested_lists_tuples_and_dicts(self):
        value = {"a": [f"x{TOKEN}", (f"y{TOKEN}",)], "n": 3, "b": None}
        redacted = redact_value(value)
        self.assertEqual(redacted, {"a": ["x[REDACTED]", ["y[REDACTED]"]], "n": 3, "b": None})

    def test_prepare_fails_closed_when_ptb_internals_change(self):
        class Opaque:
            pass

        with self.assertRaises(RuntimeError):
            prepare_request_data(Opaque())  # type: ignore[arg-type]

    def test_request_data_still_exposes_the_parameter_list(self):
        # The wrapper reads RequestData._parameters. This fails loudly if an upgrade removes it.
        data = RequestData(parameters=[])
        self.assertIsInstance(data._parameters, list)


class ProductionRequestTests(unittest.TestCase):

    def test_production_request_is_an_httpx_request_with_the_wrapper(self):
        self.assertTrue(issubclass(RedactingHTTPXRequest, HTTPXRequest))
        self.assertTrue(issubclass(RedactingHTTPXRequest, RedactingRequestMixin))
        self.assertIsNot(RedactingHTTPXRequest.do_request, HTTPXRequest.do_request)

    def test_build_application_wires_the_wrapper_and_registers_the_token(self):
        import tempfile
        from pathlib import Path
        from assistant.bot import build_application
        from test_flow import make_config

        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            config.bot_token = TOKEN
            application = build_application(config)
        normal, polling = application.bot._request  # PTB keeps both transports here
        self.assertIsInstance(normal, RedactingRequestMixin)
        self.assertIsInstance(polling, RedactingRequestMixin)
        self.assertEqual(audit.redact_secret(f"a{TOKEN}b"), "a[REDACTED]b")


class OnTheWireTests(unittest.IsolatedAsyncioTestCase):
    """A real HTTP client and a real loopback server: check the bytes that arrive."""

    async def asyncSetUp(self) -> None:
        audit.register_secret(TOKEN)
        self.received: list[tuple[str, bytes]] = []
        received = self.received

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server naming
                length = int(self.headers.get("Content-Length", "0"))
                received.append((self.path, self.rfile.read(length)))
                body = json.dumps({"ok": True, "result": MESSAGE}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:  # keep test output quiet
                return None

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

        # No proxy from the environment: the request must reach the loopback server.
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in [k for k in os.environ if "proxy" in k.lower()]:
            del os.environ[key]
        os.environ["NO_PROXY"] = "127.0.0.1,localhost"

        self.request = RedactingHTTPXRequest()
        await self.request.initialize()
        self.addAsyncCleanup(self.request.shutdown)
        base = f"http://127.0.0.1:{self.server.server_address[1]}/bot"
        self.bot = Bot(TOKEN, base_url=base, request=self.request)

    async def test_no_token_in_the_body_that_reaches_the_server(self):
        await self.bot.send_message(chat_id=1, text=f"Error while running {TOKEN}")
        self.assertEqual(len(self.received), 1)
        path, body = self.received[0]
        self.assertNotIn(TOKEN.encode("utf-8"), body)
        self.assertIn("[REDACTED]", unquote_plus(body.decode("utf-8")))  # form-encoded body
        # The token travels only in the request path, which is how Telegram authenticates.
        self.assertIn(TOKEN, path)


if __name__ == "__main__":
    unittest.main()
