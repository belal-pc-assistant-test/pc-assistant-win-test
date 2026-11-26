"""Round 4 security review: regression tests for every confirmed finding.

Each class names the finding it guards. Windows-only code (PowerShell, the
clipboard and process kill on Windows) is exercised with the platform flag and
the PowerShell runner replaced by fakes, so it runs on Linux; those paths are
NOT executed on Windows by these tests.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from _bootstrap import PROJECT_DIR
from test_flow import (FakeMessage, FakeQuery, FakeTransport, OTHER_ID, OWNER_ID,
                       make_config, make_context, make_pending, make_update)

from assistant import audit, bot as botmod, token_store
from assistant.actions import ActionContext, ActionError, execute_step
from assistant.actions import core as core_mod
from assistant.actions import system as system_mod
from assistant.actions import transfer as transfer_mod
from assistant.actions.base import (check_path_syntax, ensure_not_protected, ps_literal,
                                    resolve_in_roots, resolve_path, sanitize_filename)
from assistant.bot import FloodGate, PLAN_TTL_SECONDS
from assistant.planner import CloudPlanner, Plan, PlannerError, Step, build_plan
from assistant.remote.gate import AccessGate, generate_secret
from assistant.remote.server import RemoteServer, host_key
from assistant.remote.stream import FrameBroker

from _remote_helpers import http_request


def _decode_ps_literal(script: str) -> str:
    """Pull the base64 payload out of a ps_literal() expression and decode it."""
    match = re.search(r"FromBase64String\('([A-Za-z0-9+/=]*)'\)", script)
    assert match, f"no base64 literal in {script!r}"
    return base64.b64decode(match.group(1)).decode("utf-8")


# ---------------------------------------------------------------------------
# Path syntax: UNC, device names, alternate data streams
# ---------------------------------------------------------------------------

class PathSyntaxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = make_config(Path(self.temp.name))

    def test_unc_and_device_namespace_paths_are_refused(self):
        for raw in (r"\\attacker\share\x.txt", "//attacker/share/x.txt",
                    r"\\?\C:\Windows\win.ini", r"\\.\pipe\foo"):
            with self.subTest(raw=raw):
                with self.assertRaises(ActionError):
                    resolve_path(raw, self.config, "File path")
                with self.assertRaises(ActionError):
                    check_path_syntax(raw)

    def test_unc_paths_are_refused_by_every_file_action(self):
        unc = r"\\attacker\share\loot.txt"
        # read_file refuses everything while no folders are allowed, so give it roots here:
        # the UNC guard must still hold once reading is switched on.
        self.config.allowed_roots = [self.config.workspace_root]
        for action, params in (
            ("read_file", {"path": unc}),
            ("write_file", {"path": unc, "content": "x"}),
            ("list_dir", {"path": r"\\attacker\share"}),
            ("send_file", {"path": unc}),
        ):
            with self.subTest(action=action):
                result = execute_step(Step(action, params), self.config)
                self.assertFalse(result.ok, result.output)
                self.assertIn("UNC", result.output)

    def test_reserved_device_names_are_refused_in_any_component(self):
        for raw in ("CON", "nul", "NUL.txt", "COM1.log", "lpt9.dat", "folder/PRN.x", "aux "):
            with self.subTest(raw=raw):
                with self.assertRaises(ActionError):
                    resolve_path(raw, self.config, "File path")

    def test_names_that_only_start_like_a_device_are_allowed(self):
        resolve_path("console.txt", self.config, "File path")
        resolve_path("compile.log", self.config, "File path")

    def test_alternate_data_streams_are_refused(self):
        for raw in ("notes.txt:hidden", "C:\\data\\file.txt:Zone.Identifier"):
            with self.subTest(raw=raw):
                with self.assertRaises(ActionError):
                    resolve_path(raw, self.config, "File path")

    def test_received_names_are_sanitized(self):
        self.assertEqual(sanitize_filename(r"..\..\evil.exe"), "evil.exe")
        self.assertEqual(sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(sanitize_filename("CON.txt"), "_CON.txt")
        self.assertEqual(sanitize_filename("nul"), "_nul")
        self.assertEqual(sanitize_filename("a.txt:Zone.Identifier"), "a.txt_Zone.Identifier")
        self.assertEqual(sanitize_filename("  . "), "download.bin")

    def test_receive_file_cannot_write_to_a_device_or_stream(self):
        transport = FakeTransport(b"payload")
        ctx = ActionContext(config=self.config, transport=transport)
        for target in ("NUL", r"\\attacker\share\x.bin", "report.txt:stream"):
            with self.subTest(target=target):
                with self.assertRaises(ActionError):
                    transfer_mod.receive_file({"file_id": "f", "target": target}, ctx)
        self.assertEqual(transport.calls, [], "nothing may be downloaded for a refused target")

    def test_received_upload_with_hostile_name_lands_in_downloads(self):
        transport = FakeTransport(b"payload")
        ctx = ActionContext(config=self.config, transport=transport)
        result = transfer_mod.receive_file({"file_id": "f", "file_name": r"..\..\CON.exe", "size": "7"}, ctx)
        self.assertTrue(result.ok, result.output)
        saved = list(self.config.downloads_dir.iterdir())
        self.assertEqual([p.name for p in saved], ["_CON.exe"])


# ---------------------------------------------------------------------------
# Credential files, token folder, autostart folders, links
# ---------------------------------------------------------------------------

class ProtectedPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = make_config(self.root)
        self.token_dir = self.root / "appdata" / "TelegramPCAssistant"
        patcher = patch.object(token_store, "token_path", lambda: self.token_dir / "token.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_credential_file_names_are_refused_by_file_actions(self):
        for name in ("token.json", "TOKEN.TXT", ".env"):
            (self.root / name).write_text("secret", encoding="utf-8")
            with self.subTest(name=name):
                result = execute_step(Step("read_file", {"path": name}), self.config)
                self.assertFalse(result.ok)
                self.assertNotIn("secret", result.output)
                self.assertFalse(execute_step(Step("send_file", {"path": name}), self.config).ok)
                self.assertFalse(execute_step(Step("write_file", {"path": name, "content": "x"}),
                                              self.config).ok)

    def test_token_store_folder_is_private(self):
        self.token_dir.mkdir(parents=True)
        (self.token_dir / "notes.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(ActionError):
            resolve_path(str(self.token_dir / "notes.txt"), self.config, "File path")
        with self.assertRaises(ActionError):
            resolve_path(str(self.token_dir), self.config, "Folder path")

    def test_symlink_to_a_protected_file_is_refused(self):
        secret = self.root / "token.txt"
        secret.write_text("secret", encoding="utf-8")
        link = self.root / "innocent.txt"
        try:
            link.symlink_to(secret)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable here")
        result = execute_step(Step("read_file", {"path": "innocent.txt"}), self.config)
        self.assertFalse(result.ok)
        self.assertNotIn("secret", result.output)

    def test_writes_into_windows_startup_folder_are_refused(self):
        startup = (r"C:\Users\victim\AppData\Roaming\Microsoft\Windows"
                   r"\Start Menu\Programs\Startup\evil.bat")
        with self.assertRaises(ActionError):
            resolve_path(startup, self.config, "File path", for_write=True)
        result = execute_step(Step("write_file", {"path": startup, "content": "calc"}), self.config)
        self.assertFalse(result.ok)
        self.assertIn("Startup", result.output)

    def test_reads_of_startup_folder_are_still_allowed(self):
        # Listing or reading autostart entries is harmless; only writes are blocked.
        path = resolve_path(r"C:\Users\v\Start Menu\Programs\Startup\x.lnk", self.config, "File path")
        self.assertIsNotNone(path)

    def test_symlink_escape_out_of_allowed_roots_is_refused(self):
        root = self.root / "allowed"
        outside = self.root / "outside"
        root.mkdir()
        outside.mkdir()
        (outside / "secret.txt").write_text("nope", encoding="utf-8")
        try:
            (root / "link").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable here")
        self.config.allowed_roots = [root]
        with self.assertRaises(ActionError):
            resolve_in_roots("link/secret.txt", self.config, "File path")
        with self.assertRaises(ActionError):
            resolve_path("link/secret.txt", self.config, "File path")

    def test_ensure_not_protected_checks_the_resolved_name(self):
        with self.assertRaises(ActionError):
            ensure_not_protected(Path("/somewhere/Token.JSON"))
        ensure_not_protected(Path("/somewhere/tokens.txt"))  # not a credential name


# ---------------------------------------------------------------------------
# open action: URI schemes, UNC, scripts, shortcuts
# ---------------------------------------------------------------------------

class OpenActionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = make_config(Path(self.temp.name))
        self.ctx = ActionContext(config=self.config)
        patcher = patch.object(core_mod, "_open_external")
        self.opened = patcher.start()
        self.addCleanup(patcher.stop)

    def _refused(self, target: str) -> str:
        with self.assertRaises(ActionError) as error:
            core_mod.open_target({"target": target}, self.ctx)
        self.opened.assert_not_called()
        return str(error.exception)

    def test_uri_schemes_that_launch_tools_are_refused(self):
        for target in ("ms-msdt:/id PCWDiagnostic", "search-ms:query=x", "file:///etc/passwd",
                       "javascript:alert(1)", "shell:startup"):
            with self.subTest(target=target):
                self._refused(target)

    def test_unc_targets_are_refused(self):
        self.assertIn("UNC", self._refused(r"\\attacker\share\tool.exe"))

    def test_scripts_and_shortcuts_are_not_launched(self):
        for target in (r"C:\Users\me\Downloads\payload.ps1", "C:/x/run.bat",
                       "shortcut.lnk", r"C:\x\setup.msi"):
            with self.subTest(target=target):
                self.assertIn("script or shortcut", self._refused(target))

    def test_web_links_and_named_apps_still_open(self):
        core_mod.open_target({"target": "https://example.com/page"}, self.ctx)
        self.opened.assert_called_with("https://example.com/page")
        self.opened.reset_mock()
        core_mod.open_target({"target": "notepad"}, self.ctx)
        self.opened.assert_called_with("notepad")

    def test_token_file_cannot_be_opened(self):
        (self.config.workspace_root / "token.json").write_text("{}", encoding="utf-8")
        self._refused("token.json")


class PowerShellLiteralTests(unittest.TestCase):
    """Finding (Medium): clipboard write and (Low) process kill interpolated text
    into PowerShell with ASCII-only quote escaping. Unicode quotes break out."""

    CURLY = "\u2018\u2019\u201a\u201b"
    PAYLOAD = f"x{CURLY}; Remove-Item -Recurse C:\\ ; '\"`$(calc)"

    def test_ps_literal_round_trips_hostile_text(self):
        literal = ps_literal(self.PAYLOAD)
        self.assertEqual(_decode_ps_literal(literal), self.PAYLOAD)
        # Outside the base64 literal there is no quote, backtick or separator.
        outside = re.sub(r"'[A-Za-z0-9+/=]*'", "", literal)
        for char in self.CURLY + "\"`;$":
            self.assertNotIn(char, outside)

    def test_clipboard_write_on_windows_uses_inert_data(self):
        captured: list[str] = []

        def fake_powershell(command, timeout, cwd=None):
            captured.append(command)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.object(system_mod, "IS_WINDOWS", True), \
                patch.object(system_mod, "run_powershell", side_effect=fake_powershell):
            system_mod._clipboard_write(self.PAYLOAD)
        self.assertEqual(len(captured), 1)
        script = captured[0]
        self.assertNotIn("Remove-Item", script, "payload text must not appear in the script")
        self.assertEqual(_decode_ps_literal(script), self.PAYLOAD)

    def test_kill_process_by_name_uses_inert_data(self):
        captured: list[str] = []
        tricky = f"notepad{self.CURLY[0]}; calc"
        config = make_config(Path(tempfile.mkdtemp()))
        ctx = ActionContext(config=config)
        def fake_powershell(command, timeout, cwd=None):
            captured.append(command)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.object(system_mod, "IS_WINDOWS", True), \
                patch.object(system_mod, "is_protected_process", return_value=False), \
                patch.object(system_mod, "run_powershell", side_effect=fake_powershell):
            system_mod.kill_process({"name": tricky}, ctx)
        script = captured[0]
        self.assertNotIn("calc", script)
        self.assertIn("Stop-Process -Name ", script)
        self.assertIn(tricky, _decode_ps_literal(script))


# ---------------------------------------------------------------------------
# Logs: token redaction for every logger, bounded unauthorised logging
# ---------------------------------------------------------------------------

class LogRedactionTests(unittest.TestCase):
    def test_root_logger_output_is_redacted(self):
        secret = "424242:AAHround4_secret_token_value"
        audit.register_secret(secret)
        stream = io.StringIO()
        handler = audit.install_root_redaction(stream=stream, level=logging.WARNING)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        logging.getLogger("third_party_lib").warning("request to https://api.telegram.org/bot%s failed", secret)
        output = stream.getvalue()
        self.assertIn("[REDACTED]", output)
        self.assertNotIn(secret, output)

    def test_install_is_idempotent(self):
        first = audit.install_root_redaction(stream=io.StringIO())
        self.addCleanup(logging.getLogger().removeHandler, first)
        self.assertIs(audit.install_root_redaction(stream=io.StringIO()), first)
        count = sum(1 for h in logging.getLogger().handlers if getattr(h, "_tpa_redacting", False))
        self.assertEqual(count, 1)


class UnauthorisedAndFloodTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = make_config(Path(self.temp.name))

    def _wrapped(self):
        calls: list[int] = []

        async def handler(update, context):
            calls.append(1)

        return botmod.owner_only(handler), calls

    def test_stranger_attempts_are_logged_once_per_minute_without_text(self):
        wrapped, calls = self._wrapped()
        context = make_context(self.config)
        with patch.object(botmod.audit, "warning") as warning:
            for _ in range(5):
                asyncio.run(wrapped(make_update(message=FakeMessage("hello from a stranger"),
                                                user_id=OTHER_ID), context))
        self.assertEqual(calls, [])
        self.assertEqual(warning.call_count, 1, "a stranger must not be able to grow the log")
        self.assertNotIn("text", warning.call_args.kwargs)
        self.assertNotIn("stranger", str(warning.call_args))

    def test_flood_from_an_allowed_user_is_dropped_after_the_limit(self):
        wrapped, calls = self._wrapped()
        context = make_context(self.config)
        for _ in range(botmod.FLOOD_LIMIT + 5):
            asyncio.run(wrapped(make_update(message=FakeMessage("x")), context))
        self.assertEqual(len(calls), botmod.FLOOD_LIMIT)

    def test_flood_window_recovers(self):
        gate = FloodGate(limit=3, window=10.0)
        self.assertTrue(all(gate.allow(7, now=100.0 + i * 0.1) for i in range(3)))
        self.assertFalse(gate.allow(7, now=100.5))
        self.assertTrue(gate.allow(7, now=111.0), "old hits fall out of the window")

    def test_flood_gate_memory_is_bounded(self):
        gate = FloodGate(limit=1, window=1.0)
        for user in range(3000):
            gate.allow(user, now=float(user))
        self.assertLessEqual(len(gate._hits), 1100)


class RequestLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = make_config(Path(self.temp.name))
        self.context = make_context(self.config)

    def test_oversized_text_never_reaches_a_planner(self):
        message = FakeMessage("A" * (botmod.MAX_REQUEST_CHARS + 1))
        with patch.object(botmod, "propose_plan", new=AsyncMock()) as propose:
            asyncio.run(botmod.on_text(make_update(message=message), self.context))
        propose.assert_not_called()
        self.assertTrue(any("too long" in r for r in message.replies))
        self.assertFalse(self.context.application.bot_data["plans"])

    def test_text_at_the_limit_is_still_planned(self):
        message = FakeMessage("lock the screen " + "x" * (botmod.MAX_REQUEST_CHARS - 16))
        self.assertEqual(len(message.text), botmod.MAX_REQUEST_CHARS)
        with patch.object(botmod, "handle_request", new=AsyncMock()) as handle:
            asyncio.run(botmod.on_text(make_update(message=message), self.context))
        handle.assert_awaited_once()

    def test_pending_plan_store_is_capped(self):
        store = self.context.application.bot_data["plans"]
        for index in range(botmod.MAX_PENDING_PLANS):
            pending = make_pending(Plan(summary="x", steps=[Step("lock_screen", {})]),
                                   user_id=10_000 + index, code=f"C{index:05d}")
            pending.plan_id = f"plan{index:04d}"
            pending.created_at = time.time()
            store[pending.plan_id] = pending
        message = FakeMessage()
        result = asyncio.run(botmod.propose_plan(
            make_update(message=message), self.context,
            Plan(summary="one more", steps=[Step("lock_screen", {})])))
        self.assertIsNone(result)
        self.assertEqual(len(store), botmod.MAX_PENDING_PLANS)
        self.assertTrue(any("Too many" in r for r in message.replies))


# ---------------------------------------------------------------------------
# Approvals: exact plan, single use, expiry, no replay
# ---------------------------------------------------------------------------

class ApprovalReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = make_config(Path(self.temp.name))
        self.context = make_context(self.config)
        self.plan = Plan(summary="one", steps=[Step("lock_screen", {})])

    def test_run_plan_refuses_a_plan_that_is_no_longer_stored(self):
        pending = make_pending(self.plan)  # never stored: already claimed or never approved
        with patch.object(botmod, "_execute_claimed_plan", new=AsyncMock()) as execute:
            asyncio.run(botmod._run_plan(make_update(), self.context, pending, OWNER_ID, "button"))
        execute.assert_not_called()

    def test_run_plan_refuses_a_stale_object_with_the_same_id(self):
        pending = make_pending(self.plan)
        self.context.application.bot_data["plans"][pending.plan_id] = make_pending(self.plan)
        with patch.object(botmod, "_execute_claimed_plan", new=AsyncMock()) as execute:
            asyncio.run(botmod._run_plan(make_update(), self.context, pending, OWNER_ID, "button"))
        execute.assert_not_called()

    def test_concurrent_claims_execute_exactly_once(self):
        pending = make_pending(self.plan)
        self.context.application.bot_data["plans"][pending.plan_id] = pending

        async def race():
            await asyncio.gather(
                botmod._run_plan(make_update(), self.context, pending, OWNER_ID, "button"),
                botmod._run_plan(make_update(), self.context, pending, OWNER_ID, "button"),
            )

        with patch.object(botmod, "_execute_claimed_plan", new=AsyncMock()) as execute:
            asyncio.run(race())
        self.assertEqual(execute.await_count, 1)
        self.assertNotIn(pending.plan_id, self.context.application.bot_data["plans"])

    def test_expired_approval_button_runs_nothing(self):
        pending = make_pending(self.plan)
        pending.created_at = time.time() - PLAN_TTL_SECONDS - 5
        self.context.application.bot_data["plans"][pending.plan_id] = pending
        query = FakeQuery(f"apv:{pending.plan_id}", OWNER_ID)
        with patch.object(botmod, "_execute_claimed_plan", new=AsyncMock()) as execute:
            asyncio.run(botmod.on_button(make_update(query=query), self.context))
        execute.assert_not_called()
        self.assertTrue(any("expired" in e for e in query.edits))

    def test_replan_voids_the_earlier_code(self):
        first = asyncio.run(botmod.propose_plan(make_update(), self.context, self.plan))
        second = asyncio.run(botmod.propose_plan(make_update(), self.context,
                                                 Plan(summary="two", steps=[Step("lock_screen", {})])))
        store = self.context.application.bot_data["plans"]
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotIn(first.plan_id, store)
        self.assertIn(second.plan_id, store)

    def test_remote_start_cannot_be_run_by_the_generic_executor(self):
        result = execute_step(Step("remote_start", {"minutes": "5"}), self.config)
        self.assertFalse(result.ok)

    def test_remote_start_is_not_planner_reachable(self):
        from assistant.action_spec import BOT_ONLY_ACTIONS
        from assistant.action_spec import VALID_ACTIONS
        self.assertNotIn("remote_start", VALID_ACTIONS)
        self.assertIn("remote_start", BOT_ONLY_ACTIONS)


class VoiceTests(unittest.TestCase):
    def test_voice_transcript_cannot_approve_a_plan(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config = make_config(Path(temp.name))
        context = make_context(config)
        file_obj = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(b"audio")))
        context.bot = SimpleNamespace(get_file=AsyncMock(return_value=file_obj))
        message = FakeMessage()
        message.voice = SimpleNamespace(file_id="v1", file_size=100, duration=3)
        with patch.object(botmod, "transcribe", return_value="approve K7M2QP"), \
                patch.object(botmod, "_handle_code", new=AsyncMock()) as handle_code:
            asyncio.run(botmod.on_voice(make_update(message=message), context))
        handle_code.assert_not_called()
        self.assertTrue(any("as text" in r for r in message.replies))


# ---------------------------------------------------------------------------
# Scheduled jobs and planner output
# ---------------------------------------------------------------------------

class ScheduledJobTests(unittest.TestCase):
    def test_due_scheduled_command_only_proposes_a_plan(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config = make_config(Path(temp.name))
        context = make_context(config)
        application = context.application
        application.bot = context.bot
        job = SimpleNamespace(kind="task", chat_id=OWNER_ID, command="Remove-Item C:\\important -Recurse",
                              text="")
        with patch.object(system_mod, "run_powershell", side_effect=AssertionError("ran unapproved")), \
                patch.object(core_mod, "run_powershell", side_effect=AssertionError("ran unapproved")):
            queued = asyncio.run(botmod.queue_briefing(application, job))
        self.assertTrue(queued)
        pending = list(application.bot_data["plans"].values())
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].plan.steps[0].action, "powershell")


class PlannerAllowlistTests(unittest.TestCase):
    def test_planner_output_cannot_introduce_remote_start_or_unknown_actions(self):
        import json
        content = json.dumps({
            "summary": "helpful",
            "steps": [
                {"action": "remote_start", "params": {"minutes": "60"}, "description": "x"},
                {"action": "format_disk", "params": {"drive": "C"}, "description": "y"},
            ],
        })
        try:
            plan = build_plan(content, "cloud", "openai", 6, Path(tempfile.gettempdir()))
        except PlannerError:
            return  # rejected outright: the expected strict outcome
        actions = [step.action for step in plan.steps]
        self.assertNotIn("remote_start", actions)
        self.assertNotIn("format_disk", actions)


# ---------------------------------------------------------------------------
# Remote session: Host and Origin checks, normalized allowed names
# ---------------------------------------------------------------------------

class RemoteHostOriginTests(unittest.IsolatedAsyncioTestCase):
    HOST = "fakeone-test.trycloudflare.com"

    def test_host_key_normalizes_case_and_default_ports(self):
        self.assertEqual(host_key("ABC.trycloudflare.com:443"), "abc.trycloudflare.com")
        self.assertEqual(host_key("abc.trycloudflare.com:80"), "abc.trycloudflare.com")
        self.assertEqual(host_key("abc.trycloudflare.com:8443"), "abc.trycloudflare.com:8443")
        self.assertEqual(host_key("[::1]:8080"), "[::1]:8080")
        self.assertEqual(host_key(""), "")

    def _server(self, hosts):
        clock = [1000.0]
        gate = AccessGate(generate_secret(), deadline=clock[0] + 600, now=lambda: clock[0])
        return RemoteServer(gate=gate, broker=FrameBroker(), on_input=lambda e: None,
                            on_viewer=lambda b: None, on_sent=lambda s: None,
                            stats=lambda: {}, allowed_hosts=hosts)

    def test_allowed_host_setter_normalizes_the_public_name(self):
        server = self._server(set())
        server.allowed_hosts = {"ABC.trycloudflare.com:443"}
        self.assertEqual(server.allowed_hosts, {"abc.trycloudflare.com"})

    def test_origin_and_host_checks(self):
        server = self._server({"abc.trycloudflare.com:443"})
        self.assertTrue(server.host_ok("ABC.trycloudflare.com"))
        self.assertTrue(server.host_ok("abc.trycloudflare.com:443"))
        self.assertTrue(server.host_ok("127.0.0.1"))
        self.assertFalse(server.host_ok("evil.example"))
        self.assertFalse(server.host_ok(""))
        self.assertFalse(server.host_ok(None))
        self.assertTrue(server.origin_ok("https://ABC.trycloudflare.com", "abc.trycloudflare.com"))
        self.assertFalse(server.origin_ok("https://evil.example", "abc.trycloudflare.com"))
        self.assertFalse(server.origin_ok("null", "abc.trycloudflare.com"))
        self.assertFalse(server.origin_ok("https://abc.trycloudflare.com:8443", "abc.trycloudflare.com"))

    async def test_wrong_host_gets_403_and_does_not_consume_the_secret(self):
        clock = [1000.0]
        secret = generate_secret()
        gate = AccessGate(secret, deadline=clock[0] + 600, now=lambda: clock[0])
        denied: list[str] = []
        server = RemoteServer(gate=gate, broker=FrameBroker(), on_input=lambda e: None,
                              on_viewer=lambda b: None, on_sent=lambda s: None,
                              stats=lambda: {}, on_denied=denied.append,
                              allowed_hosts={self.HOST})
        port = await server.start()
        self.addAsyncCleanup(server.close)

        status, _, _ = await http_request(port, "GET", f"/connect/{secret}", host="evil.example")
        self.assertEqual(status, 403)
        self.assertIn("host", denied)

        status, _, _ = await http_request(port, "GET", f"/connect/{secret}", host=self.HOST,
                                          headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertIn("origin", denied)

        status, headers, _ = await http_request(port, "GET", f"/connect/{secret}", host=self.HOST)
        self.assertEqual(status, 303, "the secret must still be valid after refused Host/Origin")


# ---------------------------------------------------------------------------
# Cloud planner privacy, dependency pins, release contents
# ---------------------------------------------------------------------------

class CloudPrivacyTests(unittest.TestCase):
    def test_cloud_request_ignores_proxies_and_never_follows_redirects(self):
        import requests
        planner = CloudPlanner()
        with patch("requests.post", side_effect=requests.exceptions.ConnectionError("offline")) as post:
            with self.assertRaises(PlannerError):
                planner._post({"messages": []})
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["proxies"], {"http": "", "https": ""})
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertEqual(post.call_args.args[0], "https://text.pollinations.ai/openai")


class DependencyPinTests(unittest.TestCase):
    def test_every_runtime_requirement_is_pinned_exactly(self):
        lines = [line.strip() for line in (PROJECT_DIR / "requirements.txt").read_text(encoding='utf-8').splitlines()]
        requirements = [line for line in lines if line and not line.startswith("#")]
        self.assertTrue(requirements)
        for requirement in requirements:
            with self.subTest(requirement=requirement):
                self.assertRegex(requirement, r"^[A-Za-z0-9_.-]+==[0-9][0-9A-Za-z.+-]*$")


class StatusWiringTests(unittest.TestCase):
    def test_status_reports_the_viewer_from_the_server_property(self):
        from assistant.remote.session import RemoteController
        clock = [1000.0]
        gate = AccessGate(generate_secret(), deadline=clock[0] + 600, now=lambda: clock[0])
        server = RemoteServer(gate=gate, broker=FrameBroker(), on_input=lambda e: None,
                              on_viewer=lambda b: None, on_sent=lambda s: None, stats=lambda: {})
        session = SimpleNamespace(state="active", deadline=clock[0] + 900,
                                  deps=SimpleNamespace(now=lambda: clock[0]),
                                  server=server, provider="cloudflare")
        controller = RemoteController(is_allowed=lambda user: True, max_minutes=60,
                                      default_minutes=15, deps_factory=lambda chat: None,
                                      platform_ok=lambda: True)
        controller.active = session
        self.assertIn("Viewer: not connected", controller.status_text())
        server._viewer = object()  # what the server sets when a viewer's WebSocket is accepted
        self.assertIn("Viewer: connected", controller.status_text())


class PythonFloorTests(unittest.TestCase):
    def test_launcher_and_readme_state_the_same_python_minimum(self):
        launcher = (PROJECT_DIR / "install.bat").read_text(encoding="utf-8", errors="replace")
        readme = (PROJECT_DIR / "README.md").read_text(encoding="utf-8")
        self.assertIn("sys.version_info >= (3, 11)", launcher)
        self.assertNotIn("3.10", launcher)
        self.assertIn("Python 3.11+", readme)
        self.assertNotIn("Python 3.10+", readme)


class ReleaseContentsTests(unittest.TestCase):
    def test_release_zip_includes_shared_test_helpers_and_no_secrets(self):
        from build_zip import build_zip
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "release.zip"
            build_zip(PROJECT_DIR, archive)
            raw = set(zipfile.ZipFile(archive).namelist())
        # Entries sit under one top-level folder; compare relative paths.
        names = {n.split("/", 1)[1] if "/" in n else n for n in raw}
        self.assertIn("tests/_bootstrap.py", names)
        self.assertIn("tests/_remote_helpers.py", names)
        self.assertIn("tests/test_security_round4.py", names)
        for banned in ("token.txt", "token.json", "config.ini", ".env"):
            self.assertFalse(any(n.endswith("/" + banned) or n == banned for n in names), banned)


if __name__ == "__main__":
    unittest.main()
