"""Round 6: README and docstring claims must match the code (regression checks)."""

from __future__ import annotations

import unittest

from _bootstrap import PROJECT_DIR
assert PROJECT_DIR.is_dir()

README = (PROJECT_DIR / "README.md").read_text(encoding="utf-8")


class ReadmeClaimTests(unittest.TestCase):

    def test_python_floor_is_stated(self):
        self.assertIn("Python 3.11+", README)

    def test_voice_dependency_range_is_documented(self):
        pins = (PROJECT_DIR / "requirements-voice.txt").read_text(encoding="utf-8")
        self.assertIn("av>=12,<16", pins)
        self.assertIn("requirements-voice.txt", README)
        self.assertIn(">=12,<16", README)

    def test_token_claim_matches_the_code(self):
        # The code redacts the token from the audit log only; chat messages are not redacted.
        self.assertIn("never written to `config.ini` or to plans", README)
        self.assertIn("never paste the token into the chat", README)
        self.assertNotIn("never written to `config.ini`, plans or Telegram messages", README)

    def test_startup_retry_is_documented(self):
        self.assertRegex(README, r"retries for about 30 seconds")

    def test_token_refusal_on_windows_is_documented(self):
        self.assertIn("### Token not saved on Windows", README)

    def test_path_rules_mention_unc_and_device_forms(self):
        self.assertIn("`\\\\?\\`", README)
        self.assertIn("CONIN$", README)


class TokenStoreDocstringTests(unittest.TestCase):

    def test_docstring_lookup_order_matches_config(self):
        text = (PROJECT_DIR / "assistant" / "token_store.py").read_text(encoding="utf-8")
        doc = text.split('"""', 2)[1]
        self.assertNotIn("always wins over the store", doc)
        self.assertLess(doc.index("token.txt"), doc.index("TELEGRAM_BOT_TOKEN"))
        self.assertLess(doc.index("TELEGRAM_BOT_TOKEN"), doc.index("stored token"))
        config = (PROJECT_DIR / "assistant" / "config.py").read_text(encoding="utf-8")
        body = config[config.index("def _resolve_token"):]
        self.assertLess(body.index("token.txt"), body.index("TELEGRAM_BOT_TOKEN"))
        self.assertLess(body.index("TELEGRAM_BOT_TOKEN"), body.index("load_stored_token"))


if __name__ == "__main__":
    unittest.main()
