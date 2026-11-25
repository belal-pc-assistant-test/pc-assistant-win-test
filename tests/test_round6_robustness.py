"""Round 6 robustness: one test per confirmed bug, plus failure-mode checks.

Each bug listed here was reproduced before its fix (see the Round 6 notes).
Tests call the real code; Windows-only calls are mocked, because this sandbox is Linux.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest import mock

from _bootstrap import PROJECT_DIR
assert PROJECT_DIR.is_dir()

import requests  # noqa: E402
from telegram.error import InvalidToken, NetworkError  # noqa: E402

import main  # noqa: E402
from assistant import audit, token_store  # noqa: E402
from assistant import bot as botmod  # noqa: E402
from assistant.actions import base, dispatch  # noqa: E402
from assistant.actions.base import ActionError  # noqa: E402
from assistant.planner import (  # noqa: E402
    CloudPlanner,
    Plan,
    PlannerError,
    PlanValidationError,
    Planner,
    Step,
    build_plan,
    extract_json_object,
    sanitize_and_annotate,
)
from assistant.remote.gate import AccessGate, generate_secret  # noqa: E402
from assistant.remote.server import RemoteServer  # noqa: E402
from assistant.remote.session import RemoteController  # noqa: E402
from assistant.remote.stream import FrameBroker  # noqa: E402
from _remote_helpers import ws_open  # noqa: E402

from test_flow import (  # noqa: E402
    FakeBot,
    FakeMessage,
    make_config,
    make_context,
    make_pending,
    make_update,
    OWNER_ID,
)

TOKEN = "123456789:AAHtest_token_value_1234567890"
HOST = "fakeone-test.trycloudflare.com"


def _quiet_audit(tmp: Path) -> None:
    audit.setup(tmp / "logs" / "a.log", "CRITICAL")


# ---------------------------------------------------------------------------
# A. Two viewers at once: the single-viewer slot must be claimed before any await
# ---------------------------------------------------------------------------

class ViewerRaceTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        self.secret = generate_secret()
        self.gate = AccessGate(self.secret, deadline=10**12)
        self.server = RemoteServer(
            gate=self.gate, broker=FrameBroker(), on_input=lambda m: None,
            on_viewer=lambda c: None, on_sent=lambda s: None,
            stats=lambda: {"remaining": 1, "quality": 60, "fps_target": 8, "inputs": 0, "viewers": 0},
            allowed_hosts={HOST},
        )
        self.port = await self.server.start()
        self.addAsyncCleanup(self.server.close)

    async def test_two_simultaneous_upgrades_yield_exactly_one_viewer(self):
        status, headers, _ = await self._redeem()
        cookie = headers["set-cookie"].split("pcr_session=")[1].split(";")[0]

        original = asyncio.StreamWriter.drain

        async def slow_drain(writer_self):
            # Widen the window: the handshake flush now yields to the event loop,
            # as it does on a busy PC.
            await asyncio.sleep(0.05)
            await original(writer_self)

        with mock.patch.object(asyncio.StreamWriter, "drain", slow_drain):
            first, second = await asyncio.gather(
                ws_open(self.port, cookie, origin=f"https://{HOST}", host=HOST),
                ws_open(self.port, cookie, origin=f"https://{HOST}", host=HOST),
            )
        first[3].close()
        second[3].close()
        self.assertEqual(sorted([first[0], second[0]]), [101, 409])

    async def _redeem(self):
        from _remote_helpers import http_request
        return await http_request(self.port, "GET", f"/connect/{self.secret}", host=HOST)


# ---------------------------------------------------------------------------
# B. Deeply nested model output must be a validation error, not a RecursionError
# ---------------------------------------------------------------------------

class LoneSurrogateTests(unittest.TestCase):
    """A JSON escape such as \\ud800 decodes to a lone surrogate that UTF-8 cannot encode."""

    def test_lone_surrogate_in_plan_text_is_a_validation_error(self):
        from assistant.plan_code import fingerprint
        root = Path(tempfile.mkdtemp())
        raw = '{"summary":"s","steps":[{"action":"reply","params":{"text":"hi \\ud800 there"}}]}'
        with self.assertRaises(PlanValidationError):
            build_plan(raw, "cloud", "m", 6, root)
        ok = build_plan('{"summary":"s","steps":[{"action":"reply","params":{"text":"مرحبا"}}]}',
                        "cloud", "m", 6, root)
        self.assertTrue(fingerprint(ok.steps))


class DeepNestingTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_extract_rejects_deep_brackets_as_plan_error(self):
        for raw in ("[" * 100_000, '{"a":' * 100_000):
            with self.subTest(prefix=raw[:4]):
                with self.assertRaises(PlanValidationError):
                    extract_json_object(raw)

    def test_build_plan_rejects_deep_nesting_inside_a_plan(self):
        nested = "[" * 100_000 + "]" * 100_000
        raw = '{"summary":"x","steps":[{"action":"reply","params":{"text":' + nested + "}}]}"
        with self.assertRaises(PlanValidationError):
            build_plan(raw, "cloud", "m", 6, self.root)

    def test_planner_chain_falls_back_after_deep_cloud_reply(self):
        class DeepReplyCloud:
            def plan(self, request, root, max_steps=6):
                return build_plan("[" * 100_000, "cloud", "m", max_steps, root)

        planner = Planner(mode="cloud", cloud=DeepReplyCloud(), workspace_root=self.root)
        plan = planner.plan("take a screenshot")
        self.assertIsNotNone(plan)
        self.assertNotEqual(plan.planner, "cloud")


# ---------------------------------------------------------------------------
# C. NUL bytes in a path must not crash planning
# ---------------------------------------------------------------------------

class NulPathTests(unittest.TestCase):

    def test_nul_in_planned_paths_does_not_raise_and_is_refused_by_executor(self):
        root = Path(tempfile.mkdtemp())
        steps = [
            Step("read_file", {"path": "notes\x00.txt"}),
            Step("write_file", {"path": "C:\\a\x00b.txt", "content": "x"}),
            Step("send_file", {"path": "/tmp/a\x00"}),
        ]
        annotated, _notes = sanitize_and_annotate(steps, root)  # must not raise
        self.assertEqual(len(annotated), 3)
        with self.assertRaises(ActionError):
            base.check_path_syntax("notes\x00.txt")


# ---------------------------------------------------------------------------
# D. Telegram unreachable at startup: retry, then give up cleanly
# ---------------------------------------------------------------------------

class StartupRetryTests(unittest.TestCase):

    class FakeApp:
        def __init__(self, outcome, calls):
            self.outcome = outcome
            self.calls = calls

        def run_polling(self, **kwargs):
            self.calls.append(kwargs)
            if self.outcome is not None:
                raise self.outcome

    def test_network_errors_are_retried_then_polling_starts(self):
        calls, sleeps = [], []
        outcomes = [NetworkError("down"), NetworkError("down"), None]
        main.run_polling_with_retries(
            lambda: self.FakeApp(outcomes.pop(0), calls), sleep=sleeps.append,
        )
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [2, 4])
        self.assertEqual(calls[0]["allowed_updates"], ["message", "callback_query"])

    def test_gives_up_after_the_last_attempt_and_raises(self):
        calls, sleeps = [], []
        with self.assertRaises(NetworkError):
            main.run_polling_with_retries(
                lambda: self.FakeApp(NetworkError("down"), calls), attempts=3, sleep=sleeps.append,
            )
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [2, 4])

    def test_rejected_token_is_not_retried(self):
        calls, sleeps = [], []
        with self.assertRaises(InvalidToken):
            main.run_polling_with_retries(
                lambda: self.FakeApp(InvalidToken("nope"), calls), sleep=sleeps.append,
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])

    def test_real_library_with_unreachable_telegram_retries_then_raises(self):
        from telegram.ext import ApplicationBuilder

        def make():
            return (ApplicationBuilder().token(TOKEN)
                    .base_url("http://127.0.0.1:9/bot").build())

        sleeps: list[float] = []
        with self.assertRaises(NetworkError):
            main.run_polling_with_retries(make, attempts=2, sleep=sleeps.append)
        self.assertEqual(sleeps, [2])


# ---------------------------------------------------------------------------
# E. The planner chain must never let a backend bug escape
# ---------------------------------------------------------------------------

class PlannerChainTests(unittest.TestCase):

    def test_non_planner_exception_from_cloud_falls_back(self):
        class Broken:
            def plan(self, request, root, max_steps=6):
                raise RecursionError("deep")

        root = Path(tempfile.mkdtemp())
        planner = Planner(mode="cloud", cloud=Broken(), workspace_root=root)
        plan = planner.plan("take a screenshot")
        self.assertIsInstance(plan, Plan)
        self.assertNotEqual(plan.planner, "cloud")


# ---------------------------------------------------------------------------
# F. Retry-After values that are not finite numbers must not crash
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status, headers=None, body=None):
        self.status_code = status
        self.headers = headers or {}
        self.text = ""
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class RetryAfterTests(unittest.TestCase):

    def _post(self, header_value):
        planner = CloudPlanner(cooldown_seconds=120)
        response = _Resp(429, {"Retry-After": header_value})
        with mock.patch.object(requests, "post", return_value=response):
            with self.assertRaises(PlannerError) as ctx:
                planner._post({"model": "m", "messages": []})
        return str(ctx.exception)

    def test_nan_inf_and_overflow_use_the_default_cooldown(self):
        for raw in ("nan", "inf", "-inf", "1e400"):
            with self.subTest(raw=raw):
                message = self._post(raw)
                self.assertIn("paused for 120s", message)

    def test_numeric_hints_are_clamped(self):
        self.assertIn("paused for 15s", self._post("-5"))
        self.assertIn("paused for 600s", self._post("3600"))
        self.assertIn("paused for 45s", self._post("45"))

    def test_timeout_and_connection_errors_are_planner_errors(self):
        planner = CloudPlanner()
        with mock.patch.object(requests, "post", side_effect=requests.exceptions.Timeout()):
            with self.assertRaisesRegex(PlannerError, "timed out"):
                planner._post({})
        with mock.patch.object(requests, "post", side_effect=requests.exceptions.ConnectionError()):
            with self.assertRaisesRegex(PlannerError, "unreachable"):
                planner._post({})


# ---------------------------------------------------------------------------
# G. Windows path guard gaps (checked on Linux by calling the functions directly)
# ---------------------------------------------------------------------------

class WindowsPathGuardTests(unittest.TestCase):

    REFUSED_SYNTAX = [
        "/\\\\server\\share\\a.txt",     # mixed slash UNC
        "\\/server/share/a.txt",         # backslash then slash
        "\\??\\UNC\\server\\share\\a",   # NT object namespace
        "\\\\?\\C:\\x",                  # extended-length prefix
        "\\\\.\\pipe\\x",                # device namespace
        "//server/share/x",              # forward-slash UNC
        "C:\\x\\COM\u00b9.txt",          # superscript COM1
        "C:\\x\\LPT\u00b3.log",          # superscript LPT3
        "C:\\x\\CONIN$",                 # console input device
        "C:\\x\\CONOUT$.txt",            # console output device
    ]

    ALLOWED_SYNTAX = [
        "/tmp/ok.txt",
        "C:\\ok\\x.txt",
        "relative\\x.txt",
        "C:\\Users\\أحمد\\مستندات\\ملف 1.txt",   # Arabic folders and spaces
        "C:\\My Docs\\report (final).txt",
    ]

    def test_unc_device_and_console_forms_are_refused(self):
        for raw in self.REFUSED_SYNTAX:
            with self.subTest(path=raw):
                with self.assertRaises(ActionError):
                    base.check_path_syntax(raw)

    def test_ordinary_paths_with_spaces_and_arabic_are_allowed(self):
        for raw in self.ALLOWED_SYNTAX:
            with self.subTest(path=raw):
                base.check_path_syntax(raw)  # no exception

    def test_protected_names_with_trailing_dots_spaces_or_short_form_are_refused(self):
        for name in ("token.txt", "TOKEN.TXT", "token.txt.", "token.txt ", "TOKEN~1.TXT",
                     "token~2", "token.json", ".env"):
            with self.subTest(name=name):
                with self.assertRaises(ActionError):
                    base.ensure_not_protected(Path("C:/work") / name)

    def test_similar_but_unprotected_names_are_allowed(self):
        for name in ("tokens.txt", "mytoken.txt", "notes.txt", "token-notes.txt"):
            with self.subTest(name=name):
                base.ensure_not_protected(Path("C:/work") / name)


# ---------------------------------------------------------------------------
# H. Windows token file permissions: SID-based, checked, fail closed
# ---------------------------------------------------------------------------

SID = "S-1-5-21-111-222-333-1001"


class WindowsTokenProtectionTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "أحمد store (1)"
        self.dir.mkdir()
        self.path = self.dir / "token.json"
        self.path.write_text("{}", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_grant_is_applied_before_inheritance_is_removed(self):
        calls: list[list[str]] = []
        with mock.patch.object(token_store, "_current_user_sid", return_value=SID), \
                mock.patch.object(token_store, "_icacls", side_effect=lambda a: calls.append(a) or True):
            method = token_store._lock_down(self.path, is_windows=True)
        self.assertEqual(method, "icacls")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], str(self.path))            # Arabic and spaces stay one argument
        self.assertIn("/grant:r", calls[0])
        self.assertIn(f"*{SID}:F", calls[0])                     # SID, not the account name
        self.assertEqual(calls[1], [str(self.path), "/inheritance:r"])

    def test_failed_grant_leaves_inheritance_alone(self):
        calls: list[list[str]] = []
        with mock.patch.object(token_store, "_current_user_sid", return_value=SID), \
                mock.patch.object(token_store, "_icacls", side_effect=lambda a: calls.append(a) or False):
            method = token_store._lock_down(self.path, is_windows=True)
        self.assertEqual(method, "none")
        self.assertEqual(len(calls), 1, "no inheritance removal after a failed grant")

    def test_failed_inheritance_removal_is_reported_as_partial(self):
        results = iter([True, False])
        with mock.patch.object(token_store, "_current_user_sid", return_value=SID), \
                mock.patch.object(token_store, "_icacls", side_effect=lambda a: next(results)):
            self.assertEqual(token_store._lock_down(self.path, is_windows=True), "partial")

    def test_no_sid_means_nothing_is_changed_and_reported(self):
        with mock.patch.object(token_store, "_current_user_sid", return_value=None), \
                mock.patch.object(token_store, "_icacls") as icacls:
            self.assertEqual(token_store._lock_down(self.path, is_windows=True), "none")
        icacls.assert_not_called()

    def test_directory_hardening_grants_before_removing_inheritance(self):
        calls: list[list[str]] = []
        with mock.patch.object(token_store, "_current_user_sid", return_value=SID), \
                mock.patch.object(token_store, "_icacls", side_effect=lambda a: calls.append(a) or True):
            token_store._harden_dir(self.dir, is_windows=True)
        self.assertIn("/grant:r", calls[0])
        self.assertIn(f"*{SID}:(OI)(CI)F", calls[0])
        self.assertEqual(calls[1][1], "/inheritance:r")

    def test_sid_is_parsed_from_powershell_output_only(self):
        ok = mock.Mock(returncode=0, stdout=f"{SID}\r\n")
        with mock.patch.object(token_store.subprocess, "run", return_value=ok):
            self.assertEqual(token_store._current_user_sid(), SID)
        junk = mock.Mock(returncode=0, stdout="not a sid; rm -rf")
        with mock.patch.object(token_store.subprocess, "run", return_value=junk):
            self.assertIsNone(token_store._current_user_sid())
        failed = mock.Mock(returncode=1, stdout=SID)
        with mock.patch.object(token_store.subprocess, "run", return_value=failed):
            self.assertIsNone(token_store._current_user_sid())

    def test_save_token_refuses_to_leave_a_readable_token_on_windows(self):
        target = self.dir / "token.json"
        with mock.patch.object(token_store, "_on_windows", return_value=True), \
                mock.patch.object(token_store, "dpapi_available", return_value=False), \
                mock.patch.object(token_store, "_harden_dir"), \
                mock.patch.object(token_store, "_lock_down", return_value="none"):
            with self.assertRaisesRegex(RuntimeError, "not saved"):
                token_store.save_token(TOKEN, target)
        self.assertFalse(target.exists(), "the plain-text token must be removed")

    def test_save_token_succeeds_when_windows_lock_down_works(self):
        target = self.dir / "token.json"
        with mock.patch.object(token_store, "_on_windows", return_value=True), \
                mock.patch.object(token_store, "dpapi_available", return_value=False), \
                mock.patch.object(token_store, "_harden_dir"), \
                mock.patch.object(token_store, "_lock_down", return_value="icacls"):
            token_store.save_token(TOKEN, target)
        self.assertTrue(target.exists())

    def test_dpapi_token_is_kept_even_if_the_acl_step_fails(self):
        target = self.dir / "token.json"
        with mock.patch.object(token_store, "_on_windows", return_value=True), \
                mock.patch.object(token_store, "dpapi_available", return_value=True), \
                mock.patch.object(token_store, "_dpapi", side_effect=lambda data, encrypt: b"sealed"), \
                mock.patch.object(token_store, "_harden_dir"), \
                mock.patch.object(token_store, "_lock_down", return_value="none"):
            token_store.save_token(TOKEN, target)
        self.assertTrue(target.exists())
        self.assertEqual(token_store.protection_of(target)[:5], "DPAPI")


# ---------------------------------------------------------------------------
# Telegram drops during a plan; crashed action; restart; two quick requests
# ---------------------------------------------------------------------------

class DownBot(FakeBot):
    async def send_message(self, chat_id=None, text="", **_kwargs):
        raise NetworkError("connection dropped")

    async def edit_message_text(self, text="", **_kwargs):
        raise NetworkError("connection dropped")


class TelegramDropTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.config = make_config(self.dir)
        _quiet_audit(self.dir)

    def tearDown(self):
        audit.close()
        self.tmp.cleanup()

    def test_send_long_survives_a_dropped_connection(self):
        asyncio.run(botmod.send_long(DownBot(), OWNER_ID, "hello"))  # must not raise

    def test_plan_still_runs_and_finishes_when_telegram_drops(self):
        target = self.dir / "dropped.txt"
        plan = Plan(summary="write", steps=[Step("write_file", {"path": str(target), "content": "done"})],
                    planner="keywords")
        context = make_context(self.config)
        context.bot = DownBot()
        pending = make_pending(plan)
        context.application.bot_data["plans"][pending.plan_id] = pending
        update = make_update(message=FakeMessage())
        asyncio.run(botmod._run_plan(update, context, pending, OWNER_ID, "button"))
        self.assertEqual(target.read_text(encoding="utf-8"), "done")
        self.assertNotIn(pending.plan_id, context.application.bot_data["plans"])


class CrashedActionTests(unittest.TestCase):

    def test_a_crashing_handler_is_reported_and_does_not_raise(self):
        tmp = Path(tempfile.mkdtemp())
        _quiet_audit(tmp)
        try:
            config = make_config(tmp)

            def boom(params, context):
                raise RuntimeError("handler exploded")

            with mock.patch.dict(dispatch.HANDLERS, {"reply": boom}):
                result = dispatch.execute_step(Step("reply", {"text": "x"}), config)
            self.assertFalse(result.ok)
            self.assertIn("Unexpected error", result.output)
        finally:
            audit.close()


class LogRotationTests(unittest.TestCase):

    def test_log_rotates_and_keeps_only_the_configured_backups(self):
        tmp = Path(tempfile.mkdtemp())
        log = tmp / "a.log"
        audit.setup(log, "INFO")
        try:
            handler = next(h for h in logging.getLogger("assistant").handlers
                           if isinstance(h, RotatingFileHandler))
            self.assertEqual(handler.backupCount, 5)
            handler.maxBytes = 4_000          # small, so the test rotates quickly
            for index in range(600):
                audit.event("filler", n=index, pad="x" * 60)
        finally:
            audit.close()
        names = sorted(p.name for p in tmp.iterdir())
        self.assertIn("a.log.1", names)
        self.assertLessEqual(len([n for n in names if n.startswith("a.log")]), 6)


class QuickRequestsAndRestartTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.config = make_config(self.dir)
        _quiet_audit(self.dir)

    def tearDown(self):
        audit.close()
        self.tmp.cleanup()

    def _say(self, context, text):
        message = FakeMessage(text)
        asyncio.run(botmod.on_text(make_update(message=message), context))
        return message

    def test_two_quick_requests_leave_only_the_newest_plan_approvable(self):
        context = make_context(self.config, planner=Planner(mode="keywords", workspace_root=self.dir))
        self._say(context, "take a screenshot")
        first = list(context.application.bot_data["plans"].values())
        self._say(context, "take a screenshot")
        plans = list(context.application.bot_data["plans"].values())
        self.assertTrue(first)
        self.assertEqual(len(plans), 1, "a newer request supersedes the older pending plan")
        self.assertNotEqual(plans[0].code, first[0].code)

    def test_pending_plan_does_not_run_after_restart_without_approval(self):
        target = self.dir / "after_restart.txt"
        plan = Plan(summary="write", steps=[Step("write_file", {"path": str(target), "content": "x"})],
                    planner="keywords")
        before = make_context(self.config)
        pending = make_pending(plan, code="K7M2QP")
        before.application.bot_data["plans"][pending.plan_id] = pending

        after = make_context(self.config)               # new process: nothing in memory
        message = self._say(after, "approve K7M2QP")
        self.assertFalse(target.exists(), "a restart must never run a pending plan")
        self.assertEqual(after.application.bot_data["plans"], {})
        self.assertTrue(message.replies, "the user is told the code is not valid")

    def test_pending_plans_are_never_written_to_disk(self):
        context = make_context(self.config, planner=Planner(mode="keywords", workspace_root=self.dir))
        self._say(context, "take a screenshot")
        code = next(iter(context.application.bot_data["plans"].values())).code
        hits = []
        for path in self.dir.rglob("*"):
            if path.is_file() and code in path.read_bytes().decode("utf-8", "ignore"):
                hits.append(path.name)
        self.assertEqual(hits, [], "pending codes live in memory only")

    def test_remote_session_is_not_resumed_after_restart(self):
        controller = RemoteController(
            is_allowed=lambda uid: True, max_minutes=60, default_minutes=15,
            deps_factory=lambda uid: None, platform_ok=lambda: True,
        )
        self.assertFalse(controller.running())


# ---------------------------------------------------------------------------
# Windows-only code paths that must degrade, not crash, on Linux
# ---------------------------------------------------------------------------

class PlatformStubTests(unittest.TestCase):

    def test_lock_down_on_posix_uses_chmod_and_reports_it(self):
        tmp = Path(tempfile.mkdtemp())
        target = tmp / "t.json"
        target.write_text("{}", encoding="utf-8")
        self.assertEqual(token_store._lock_down(target, is_windows=False), "chmod600")
        self.assertEqual(oct(target.stat().st_mode & 0o777), oct(0o600))


if __name__ == "__main__":
    unittest.main()
