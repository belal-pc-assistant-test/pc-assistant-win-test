"""Audit reason labels: short lowercase labels are logged, anything else is dropped."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from _bootstrap import PROJECT_DIR  # also makes the project importable

assert PROJECT_DIR.is_dir()

from assistant import audit
from assistant import bot as bot_module


class AuditReasonTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.log_file = Path(self._tmp.name) / "assistant.log"
        audit.setup(self.log_file)

    def tearDown(self) -> None:
        audit.close()
        self._tmp.cleanup()

    def _lines(self) -> list[dict]:
        for handler in audit._LOGGER.handlers:
            handler.flush()
        text = self.log_file.read_text(encoding="utf-8") if self.log_file.exists() else ""
        # Lines carry a logging prefix before the JSON payload.
        return [json.loads(line[line.index("{"):]) for line in text.splitlines() if "{" in line]

    def test_stranger_warning_keeps_user_id_and_reason_label(self) -> None:
        audit.warning("Unauthorised access attempt ignored", user_id=424242, reason="unauthorised user")
        (entry,) = self._lines()
        self.assertEqual(entry["event"], "warning")
        self.assertEqual(entry["user_id"], 424242)
        self.assertEqual(entry["reason"], "unauthorised user")
        self.assertNotIn("Unauthorised access attempt", self.log_file.read_text(encoding="utf-8"))

    def test_error_event_keeps_only_the_reason_label(self) -> None:
        audit.error("Telegram handler error", exc_info=True, reason="network error")
        (entry,) = self._lines()
        self.assertEqual(entry["event"], "error")
        self.assertEqual(entry["reason"], "network error")

    def test_reason_that_looks_like_a_token_is_dropped(self) -> None:
        secret = "123456789:AAHabcDEF_ghiJKLmnoPQR-stuVWXyz"
        audit.warning("x", user_id=7, reason=secret)
        audit.error("boom", exc_info=True, reason=f"see https://api.telegram.org/bot{secret}")
        text = self.log_file.read_text(encoding="utf-8")
        self.assertNotIn(secret, text)
        self.assertNotIn("api.telegram.org", text)
        for entry in self._lines():
            self.assertNotIn("reason", entry)

    def test_error_message_text_is_never_logged(self) -> None:
        audit.error("secret detail 987654:AAsecretvalue", exc_info=True)
        text = self.log_file.read_text(encoding="utf-8")
        self.assertNotIn("secret detail", text)
        self.assertNotIn("AAsecretvalue", text)


class ErrorLabelTests(unittest.TestCase):
    def test_telegram_exception_classes_become_readable_labels(self) -> None:
        from telegram.error import Conflict, NetworkError, TimedOut
        self.assertEqual(bot_module._error_label(Conflict("terminated by other getUpdates")), "conflict")
        self.assertEqual(bot_module._error_label(TimedOut("read timed out")), "timed out")
        self.assertEqual(bot_module._error_label(NetworkError("httpx failure at https://x")), "network error")

    def test_none_and_unsafe_class_names_become_unknown(self) -> None:
        self.assertEqual(bot_module._error_label(None), "unknown")
        UnsafeName = type("Token_123:AAx", (Exception,), {})
        self.assertEqual(bot_module._error_label(UnsafeName("boom")), "unknown")

    def test_labels_always_pass_the_audit_filter(self) -> None:
        for exc in (None, ValueError("x"), KeyError("y")):
            label = bot_module._error_label(exc)
            self.assertRegex(label, audit._SAFE_TEXT_RE.pattern)
            self.assertIsNotNone(audit._SAFE_TEXT_RE.fullmatch(label), label)


if __name__ == "__main__":
    unittest.main()
