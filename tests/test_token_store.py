"""Tests for the token store: first-run storage, masking, permissions.

The DPAPI branch can only run on Windows; here we test the portable fallback
(plain file + owner-only permissions) plus the format/permission guarantees.
"""

from __future__ import annotations

import base64
import json
import shutil
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

_ISOLATED = tempfile.mkdtemp(prefix="tpa-tokenstore-")
os.environ["TPA_TOKEN_STORE_DIR"] = _ISOLATED

from assistant import token_store                                 # noqa: E402

FAKE_TOKEN = "8123456789:AAH8xQz3kQ9zvABCDEFGHIJKLMNOPQRSTUVW"


class TokenStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "token.json"

    def tearDown(self):
        self.tmp.cleanup()

    # -- format ---------------------------------------------------------
    def test_token_format_validation(self):
        self.assertTrue(token_store.valid_token_format(FAKE_TOKEN))
        self.assertFalse(token_store.valid_token_format(""))
        self.assertFalse(token_store.valid_token_format("not-a-token"))
        self.assertFalse(token_store.valid_token_format("123:short"))

    def test_mask_never_reveals_the_secret(self):
        masked = token_store.mask_token(FAKE_TOKEN)
        self.assertIn("8123456789", masked)
        self.assertNotIn("ABCDEFGHIJKLMNOP", masked)
        self.assertTrue(masked.endswith("UVW"))
        self.assertEqual(token_store.mask_token(""), "(none)")

    # -- save / load ----------------------------------------------------
    def test_roundtrip(self):
        saved = token_store.save_token(FAKE_TOKEN, path=self.path)
        self.assertEqual(saved, self.path)
        self.assertTrue(self.path.exists())
        self.assertEqual(token_store.load_stored_token(self.path), FAKE_TOKEN)

    def test_file_is_owner_only(self):
        token_store.save_token(FAKE_TOKEN, path=self.path)
        mode = stat.S_IMODE(self.path.stat().st_mode)
        if os.name != "nt":
            self.assertEqual(mode, 0o600, f"file mode was {oct(mode)}")

    def test_file_never_contains_the_plain_token_when_protected(self):
        token_store.save_token(FAKE_TOKEN, path=self.path)
        raw = self.path.read_text(encoding="utf-8")
        data = json.loads(raw)
        self.assertEqual(data["version"], 1)
        self.assertIn(data["protection"], {"dpapi", "plain"})
        if data["protection"] == "dpapi":
            self.assertNotIn("AAH8xQz3kQ9zv", raw)
            # payload must decode to binary, not to the token
            self.assertNotEqual(
                base64.b64decode(data["token"]).decode("utf-8", "replace"), FAKE_TOKEN
            )

    def test_save_rejects_garbage(self):
        with self.assertRaises(ValueError):
            token_store.save_token("", path=self.path)
        with self.assertRaises(ValueError):
            token_store.save_token("hello world", path=self.path)
        self.assertFalse(self.path.exists())

    def test_delete(self):
        token_store.save_token(FAKE_TOKEN, path=self.path)
        self.assertTrue(token_store.delete_token(self.path))
        self.assertFalse(self.path.exists())
        self.assertFalse(token_store.delete_token(self.path))

    # -- robustness -----------------------------------------------------
    def test_missing_file_returns_none(self):
        self.assertIsNone(token_store.load_stored_token(self.dir / "nope.json"))

    def test_corrupt_file_returns_none(self):
        for content in ("", "{not json", "[]", '{"version":1}', '{"token": 5}'):
            self.path.write_text(content, encoding="utf-8")
            self.assertIsNone(token_store.load_stored_token(self.path), content)

    def test_bad_base64_returns_none(self):
        self.path.write_text(
            json.dumps({"version": 1, "protection": "plain", "token": "!!!not-b64!!!"}),
            encoding="utf-8",
        )
        self.assertIsNone(token_store.load_stored_token(self.path))

    def test_dpapi_blob_on_wrong_machine_returns_none(self):
        # Simulates a token copied from another PC: unreadable, not a crash.
        self.path.write_text(
            json.dumps({"version": 1, "protection": "dpapi",
                        "token": base64.b64encode(b"garbage-bytes").decode()}),
            encoding="utf-8",
        )
        self.assertIsNone(token_store.load_stored_token(self.path))

    def test_protection_description(self):
        token_store.save_token(FAKE_TOKEN, path=self.path)
        text = token_store.protection_of(self.path)
        self.assertTrue(text)
        self.assertIn(token_store.protection_of(self.path), text)

    def test_store_dir_can_be_relocated(self):
        # Set the folder for this test only: other test modules also set the variable at import time.
        other = tempfile.mkdtemp(prefix="tpa-relocate-")
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        with mock.patch.dict(os.environ, {"TPA_TOKEN_STORE_DIR": other}):
            self.assertEqual(token_store.store_dir(), Path(other).resolve())

    def test_validation_offline_is_graceful(self):
        # Empty/garbage tokens are rejected without any network call.
        ok, message, result = token_store.validate_token("nonsense")
        self.assertFalse(ok)
        self.assertIsNone(result)
        self.assertIn("bot token", message.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
