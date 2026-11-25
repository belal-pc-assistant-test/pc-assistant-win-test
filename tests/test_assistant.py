"""Offline tests — no Telegram, no network, no Ollama required.

Run from the project folder:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

# Never touch a real stored token while testing.
_ISOLATED_STORE = tempfile.mkdtemp(prefix="tpa-store-")
os.environ["TPA_TOKEN_STORE_DIR"] = _ISOLATED_STORE
os.environ.pop("TELEGRAM_BOT_TOKEN", None)

from assistant import actions                                    # noqa: E402
from assistant.config import ConfigError, load_config            # noqa: E402
from assistant.planner import Plan, Step, rule_based_plan        # noqa: E402


def make_config(tmp: Path, *, token: str = "123456789:AAHtest_token_value_1234567890",
                ids: str = "42, 43", extra_planner: str = ""):
    """Write a throw-away config.ini and load it."""
    cfg = tmp / "config.ini"
    token_line = f"bot_token = {token}\n" if token is not None else "bot_token =\n"
    cfg.write_text(
        "[telegram]\n"
        f"{token_line}"
        f"allowed_user_ids = {ids}\n"
        "\n[planner]\n"
        "mode = keywords\n"
        f"{extra_planner}"
        "\n[ollama]\n"
        "use_ollama = false\n"
        "base_url = http://127.0.0.1:11434\n"
        "model = qwen2.5:3b\n"
        "\n[behaviour]\n"
        "command_timeout = 5\n"
        "max_output_chars = 500\n"
        "blocked_patterns = forbidden-flag\n"
        "\n[paths]\n"
        f"workspace_root = {tmp.as_posix()}\n"
        f"log_file = {(tmp / 'logs' / 'a.log').as_posix()}\n"
        f"screenshots_dir = {(tmp / 'shots').as_posix()}\n",
        encoding="utf-8",
    )
    return load_config(cfg)


class ConfigTests(unittest.TestCase):
    def test_loads_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            self.assertEqual(sorted(cfg.allowed_user_ids), [42, 43])
            self.assertTrue(cfg.is_allowed(42))
            self.assertFalse(cfg.is_allowed(99))
            self.assertFalse(cfg.is_allowed(None))
            self.assertFalse(cfg.use_ollama)
            self.assertEqual(cfg.command_timeout, 5)
            self.assertEqual(cfg.blocked_patterns, ["forbidden-flag"])
            self.assertEqual(cfg.planner_mode, "keywords")

    def test_legacy_config_token_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            self.assertEqual(cfg.bot_token, "")
            self.assertEqual(cfg.token_source, "")

    def test_env_token_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["TELEGRAM_BOT_TOKEN"] = "999999:AAHenvironment_token_value_12345678"
            try:
                cfg = make_config(Path(tmp))
            finally:
                os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            self.assertTrue(cfg.bot_token.startswith("999999:"))
            self.assertEqual(cfg.token_source, "env")

    def test_missing_token_is_not_an_error(self):
        """First run: the token is requested later by main.py."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp), token=None)
            self.assertEqual(cfg.bot_token, "")
            self.assertEqual(cfg.token_source, "")

    def test_malformed_token_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["TELEGRAM_BOT_TOKEN"] = "not-a-token"
            try:
                with self.assertRaises(ConfigError):
                    make_config(Path(tmp), token=None)
            finally:
                os.environ.pop("TELEGRAM_BOT_TOKEN", None)

    def test_missing_file_raises(self):
        with self.assertRaises(ConfigError):
            load_config("/nonexistent/config.ini")

    def test_empty_ids_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError):
                make_config(Path(tmp), ids="")

    def test_bad_planner_mode_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "bad.ini"
            cfg.write_text(
                "[telegram]\nallowed_user_ids = 42\n[planner]\nmode = magic\n",
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_config(cfg)

    def test_planner_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "c.ini"
            cfg.write_text(
                "[telegram]\nallowed_user_ids = 42\n"
                "[planner]\nmode = cloud\n"
                "[ollama]\nuse_ollama = true\n",
                encoding="utf-8",
            )
            loaded = load_config(cfg)
            self.assertEqual(loaded.planner_chain(), ["cloud", "local", "keywords"])
            self.assertTrue(loaded.uses_cloud)

            cfg.write_text(
                "[telegram]\nallowed_user_ids = 42\n"
                "[planner]\nmode = local\n"
                "[ollama]\nuse_ollama = false\n",
                encoding="utf-8",
            )
            self.assertEqual(load_config(cfg).planner_chain(), ["keywords"])

            cfg.write_text(
                "[telegram]\nallowed_user_ids = 42\n[planner]\nmode = keywords\n",
                encoding="utf-8",
            )
            self.assertEqual(load_config(cfg).planner_chain(), ["keywords"])

    def test_token_can_come_from_the_encrypted_store(self):
        """No token in config.ini + a stored token -> source == 'store'."""
        from assistant import token_store

        with tempfile.TemporaryDirectory() as tmp:
            store_dir = Path(tmp) / "store"
            old = os.environ.get("TPA_TOKEN_STORE_DIR")
            os.environ["TPA_TOKEN_STORE_DIR"] = str(store_dir)
            try:
                token_store.save_token("555555:AAHstored_token_value_123456789")
                cfg_path = Path(tmp) / "config.ini"
                cfg_path.write_text(
                    "[telegram]\nbot_token =\nallowed_user_ids = 42\n"
                    "[planner]\nmode = keywords\n",
                    encoding="utf-8",
                )
                loaded = load_config(cfg_path)
                self.assertEqual(loaded.bot_token, "555555:AAHstored_token_value_123456789")
                self.assertEqual(loaded.token_source, "store")
                self.assertEqual(loaded.token_store_path, store_dir.resolve() / "token.json")
            finally:
                if old is None:
                    os.environ.pop("TPA_TOKEN_STORE_DIR", None)
                else:
                    os.environ["TPA_TOKEN_STORE_DIR"] = old

    def test_shipped_example_has_no_token_and_your_id(self):
        """The template that ships in the zip must not contain a secret."""
        example = (PROJECT_DIR / "config.example.ini").read_text(encoding="utf-8")
        self.assertIn("allowed_user_ids = 7823157087", example)
        self.assertIn("mode = cloud", example)
        self.assertIn("bot_token =\n", example.replace("\r\n", "\n"))
        for line in example.splitlines():
            if line.strip().startswith("bot_token"):
                self.assertEqual(line.split("=", 1)[1].strip(), "")


class RulePlannerTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path("/tmp/ws")

    def plan(self, text):
        return rule_based_plan(text, self.ws)

    def test_screenshot_english(self):
        plan = self.plan("take a screenshot please")
        self.assertEqual([s.action for s in plan.steps], ["screenshot"])
        self.assertEqual(plan.planner, "keywords")

    def test_screenshot_arabic(self):
        plan = self.plan("خد لقطة شاشة")
        self.assertEqual(plan.steps[0].action, "screenshot")

    def test_open_chrome_arabic_alias(self):
        plan = self.plan("افتح كروم")
        self.assertEqual(plan.steps[0].action, "open")
        self.assertEqual(plan.steps[0].params["target"], "chrome")

    def test_open_path(self):
        plan = self.plan(r"open C:\Users\me\Desktop\report.docx")
        self.assertEqual(plan.steps[0].action, "open")
        self.assertIn("report.docx", plan.steps[0].params["target"])

    def test_list_dir(self):
        plan = self.plan(r"list files in C:\Users\me\Desktop")
        self.assertEqual(plan.steps[0].action, "list_dir")
        self.assertIn("Desktop", plan.steps[0].params["path"])

    def test_read_file(self):
        plan = self.plan(r"read C:\Users\me\notes.txt")
        self.assertEqual(plan.steps[0].action, "read_file")
        self.assertIn("notes.txt", plan.steps[0].params["path"])

    def test_write_file(self):
        plan = self.plan(r"write C:\tmp\a.txt :: hello world")
        self.assertEqual(plan.steps[0].action, "write_file")
        self.assertEqual(plan.steps[0].params["content"], "hello world")

    def test_write_file_arabic(self):
        plan = self.plan("اكتب في C:\\tmp\\a.txt :: مرحبا")
        self.assertEqual(plan.steps[0].action, "write_file")
        self.assertEqual(plan.steps[0].params["content"], "مرحبا")

    def test_raw_command_prefix(self):
        plan = self.plan("!Get-Process | Select -First 3")
        self.assertEqual(plan.steps[0].action, "powershell")
        self.assertIn("Get-Process", plan.steps[0].params["command"])

    def test_raw_command_detection(self):
        plan = self.plan("ipconfig /all")
        self.assertEqual(plan.steps[0].action, "powershell")

    def test_unknown_request_is_reply_only(self):
        plan = self.plan("tell me a joke about cats")
        self.assertEqual(plan.steps[0].action, "reply")
        self.assertFalse(plan.needs_action)
        self.assertIsNotNone(plan.reply_text)


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.config = make_config(self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def run_step(self, action: str, **params):
        """Go through the dispatcher, which is what the bot uses."""
        return actions.execute_step(Step(action, params), self.config)

    def test_write_then_read_roundtrip(self):
        target = self.dir / "note.txt"
        res = self.run_step("write_file", path=str(target), content="مرحبا hello")
        self.assertTrue(res.ok, res.output)
        self.assertTrue(target.exists())

        res = self.run_step("read_file", path=str(target))
        self.assertTrue(res.ok, res.output)
        self.assertIn("مرحبا hello", res.output)

    def test_relative_path_resolves_into_workspace(self):
        res = self.run_step("write_file", path="sub/rel.txt", content="x")
        self.assertTrue(res.ok, res.output)
        self.assertTrue((self.dir / "sub" / "rel.txt").exists())

    def test_read_missing_file(self):
        res = self.run_step("read_file", path=str(self.dir / "nope.txt"))
        self.assertFalse(res.ok)
        self.assertIn("not found", res.output.lower())

    def test_list_dir(self):
        (self.dir / "alpha.txt").write_text("a", encoding="utf-8")
        (self.dir / "beta").mkdir(exist_ok=True)
        res = self.run_step("list_dir", path=str(self.dir))
        self.assertTrue(res.ok, res.output)
        self.assertIn("alpha.txt", res.output)
        self.assertIn("beta", res.output)

    def test_large_file_refused(self):
        big = self.dir / "big.txt"
        big.write_text("x" * (300 * 1024), encoding="utf-8")
        res = self.run_step("read_file", path=str(big))
        self.assertFalse(res.ok)
        self.assertIn("larger", res.output)

    def test_allowed_roots_restriction(self):
        self.config.allowed_roots = [self.dir / "inner"]
        (self.dir / "inner").mkdir(exist_ok=True)
        ok = self.run_step("write_file", path=str(self.dir / "inner" / "a.txt"), content="x")
        self.assertTrue(ok.ok, ok.output)
        blocked = self.run_step("write_file", path=str(self.dir / "outside.txt"), content="x")
        self.assertFalse(blocked.ok)
        self.assertIn("allowed", blocked.output.lower())

    def test_blocked_pattern_refused(self):
        res = self.run_step("powershell", command="echo forbidden-flag")
        self.assertFalse(res.ok)
        self.assertIn("block", res.output.lower())

    def test_powershell_missing_or_runs(self):
        res = self.run_step("powershell", command="echo hello")
        # On Windows this succeeds; on Linux it reports a clean error instead
        # of crashing the bot.
        self.assertIsInstance(res.output, str)
        self.assertTrue(res.output)

    def test_dispatch_unknown_action(self):
        res = actions.execute_step(Step("nonsense", {}), self.config)
        self.assertFalse(res.ok)
        self.assertIn("unsupported", res.output.lower())

    def test_dispatch_reply(self):
        res = actions.execute_step(Step("reply", {"text": "hello"}), self.config)
        self.assertTrue(res.ok)
        self.assertEqual(res.output, "hello")

    def test_never_raises(self):
        res = actions.execute_step(Step("read_file", {"path": None}), self.config)
        self.assertFalse(res.ok)

    def test_preview_lines(self):
        step = Step("powershell", {"command": "Get-Date"}, "show date")
        preview = actions.step_preview(step, 1)
        self.assertIn("1.", preview)
        self.assertIn("Get-Date", preview)


class BotFormattingTests(unittest.TestCase):
    def test_plan_message_shows_planner_label_and_escapes_html(self):
        from assistant.bot import render_plan_message

        plan = Plan(
            summary="run <script>",
            steps=[Step("powershell", {"command": "echo <b>&</b>"})],
            planner="cloud",
            model="openai",
        )
        text = render_plan_message(plan, "K7M2QP", "9f3a1c02")
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("&lt;b&gt;", text)
        self.assertIn("cloud", text)
        self.assertIn("pollinations", text)
        self.assertIn("Nothing has run yet", text)
        self.assertIn("K7M2QP", text)          # plan code
        self.assertIn("9f3a1c02", text)        # fingerprint of the exact steps

    def test_local_label_mentions_ollama(self):
        from assistant.bot import render_plan_message

        plan = Plan("s", [Step("screenshot", {})], planner="local", model="qwen2.5:3b")
        text = render_plan_message(plan, "K7M2QP", "9f3a1c02")
        self.assertIn("local", text)
        self.assertIn("Ollama", text)

    def test_keyword_label(self):
        from assistant.bot import render_plan_message

        plan = Plan("s", [Step("screenshot", {})], planner="keywords")
        self.assertIn("keywords", render_plan_message(plan, "K7M2QP", "9f3a1c02"))

    def test_step_warning_is_rendered(self):
        from assistant.bot import render_plan_message

        step = Step("read_file", {"path": r"C:\nope.txt"}, warning="⚠️ does not exist")
        text = render_plan_message(Plan("s", [step], planner="cloud", model="openai"), "K7M2QP", "9f3a1c02")
        self.assertIn("does not exist", text)

    def test_keyboard_has_yes_and_no(self):
        from assistant.bot import build_keyboard

        kb = build_keyboard("abc123")
        buttons = [b for row in kb.inline_keyboard for b in row]
        datas = [b.callback_data for b in buttons]
        self.assertIn("apv:abc123", datas)
        self.assertIn("deny:abc123", datas)


if __name__ == "__main__":
    unittest.main(verbosity=2)
