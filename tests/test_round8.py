"""Round 8 tests: read_file fail-closed, real-path send/receive checks, upload size,
/pause and /resume, the Administrator refusal, and the Windows ACL command shape.

Everything here runs on Linux. The Windows-only parts live in test_windows_real.py.
Network calls are stubbed: no test talks to Telegram.
"""
from __future__ import annotations

import asyncio
import base64
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_flow import (  # noqa: E402  (shared fakes; also sets a private token store)
    FakeBot, FakeMessage, FakeQuery, FakeTransport, OWNER_ID, OTHER_ID,
    make_config, make_context, make_update,
)

from _hermetic import IsolatedCase, ModuleGuard  # noqa: E402

from telegram.ext import ApplicationHandlerStop  # noqa: E402

from assistant import bot as botmod  # noqa: E402
from assistant import elevation, pause, token_store  # noqa: E402
from assistant.actions import core as core_mod  # noqa: E402
from assistant.actions import transfer as transfer_mod  # noqa: E402
from assistant.actions.base import ActionContext, ActionError  # noqa: E402

import main as main_mod  # noqa: E402

SECRET_TEXT = "TOP-SECRET-CONTENT-123"


# No test in this module may start a real process or open a network connection (tests/_hermetic.py).
# main() also changes process-wide state on Windows (console code page, DPI awareness, root logging),
# so those are no-ops here.
_guard = ModuleGuard(extra=[
    (main_mod, "ensure_utf8_console", lambda: None),
    (main_mod, "enable_dpi_awareness", lambda: None),
    (botmod.audit, "install_root_redaction", lambda *_a, **_k: None),
])


def setUpModule():
    _guard.start()


def tearDownModule():
    _guard.stop()


def _symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        raise unittest.SkipTest(f"symlinks are not available here: {exc}")


class _Tmp(IsolatedCase):
    def setUp(self) -> None:
        super().setUp()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmpdir.name)
        self.config = make_config(self.dir)
        # The workspace is a subfolder, so "outside" really is outside the roots.
        self.ws = self.dir / "ws"
        self.ws.mkdir(parents=True, exist_ok=True)
        self.config.workspace_root = self.ws
        self.config.downloads_dir = self.ws / "downloads"
        self.outside = self.dir / "outside"
        self.outside.mkdir()
        self.secret = self.outside / "secret.txt"
        self.secret.write_text(SECRET_TEXT, encoding="utf-8")
        botmod.audit.setup(self.dir / "logs" / "a.log", "CRITICAL")

    def tearDown(self) -> None:
        botmod.audit.close()
        self.tmpdir.cleanup()


# ---------------------------------------------------------------------------
# D1: read_file fails closed
# ---------------------------------------------------------------------------

class ReadFileFailClosedTests(_Tmp):

    def ctx(self):
        return ActionContext(config=self.config)

    def test_refuses_every_file_when_no_allowed_folders_are_set(self):
        self.config.allowed_roots = []
        (self.ws / "notes.txt").write_text("harmless", encoding="utf-8")
        for path in (str(self.ws / "notes.txt"), str(self.secret)):
            with self.subTest(path=path), self.assertRaises(ActionError) as caught:
                core_mod.read_file({"path": path}, self.ctx())
            self.assertIn("allowed_roots", str(caught.exception))
            self.assertNotIn(SECRET_TEXT, str(caught.exception))

    def test_symlink_out_of_the_roots_is_refused_when_roots_are_set(self):
        self.config.allowed_roots = [self.ws]
        link = self.ws / "link.txt"
        _symlink(link, self.secret)
        with self.assertRaises(ActionError):
            core_mod.read_file({"path": str(link)}, self.ctx())

    def test_file_inside_the_roots_is_still_read(self):
        self.config.allowed_roots = [self.ws]
        (self.ws / "notes.txt").write_text("hello there", encoding="utf-8")
        result = core_mod.read_file({"path": str(self.ws / "notes.txt")}, self.ctx())
        self.assertTrue(result.ok)
        self.assertIn("hello there", result.output)


# ---------------------------------------------------------------------------
# D2: send_file checks the real path (symlinks and directory links resolve first)
# ---------------------------------------------------------------------------

class SendFileRealPathTests(_Tmp):

    def test_file_symlink_pointing_outside_is_refused_with_or_without_roots(self):
        link = self.ws / "link.txt"
        _symlink(link, self.secret)
        for roots in ([], [self.ws]):
            self.config.allowed_roots = roots
            with self.subTest(roots=roots), self.assertRaises(ActionError):
                transfer_mod.send_file({"path": "link.txt"}, ActionContext(config=self.config))

    def test_folder_symlink_pointing_outside_is_refused(self):
        _symlink(self.ws / "dirlink", self.outside, directory=True)
        with self.assertRaises(ActionError):
            transfer_mod.send_file({"path": "dirlink/secret.txt"}, ActionContext(config=self.config))

    def test_a_real_file_inside_the_roots_is_sent(self):
        (self.ws / "report.txt").write_text("ok", encoding="utf-8")
        result = transfer_mod.send_file({"path": "report.txt"}, ActionContext(config=self.config))
        self.assertTrue(result.ok)


# ---------------------------------------------------------------------------
# D3: uploads are size-checked before download, and the destination is checked
# ---------------------------------------------------------------------------

class ReceiveFileTests(_Tmp):

    def run_receive(self, params, transport):
        ctx = ActionContext(config=self.config, transport=transport)
        return transfer_mod.receive_file(params, ctx)

    def test_missing_size_is_refused_before_any_download(self):
        transport = FakeTransport(b"payload")
        with self.assertRaises(ActionError) as caught:
            self.run_receive({"file_id": "f", "file_name": "a.bin"}, transport)
        self.assertIn("file size", str(caught.exception))
        self.assertEqual(transport.calls, [])

    def test_non_numeric_size_is_refused(self):
        for raw in ("", "abc", "-5", "1.5"):
            with self.subTest(size=raw):
                transport = FakeTransport(b"payload")
                with self.assertRaises(ActionError):
                    self.run_receive({"file_id": "f", "file_name": "a.bin", "size": raw}, transport)
                self.assertEqual(transport.calls, [])

    def test_size_over_the_limit_is_refused_before_download(self):
        transport = FakeTransport(b"payload")
        limit = self.config.max_send_file_mb * 1024 * 1024
        with self.assertRaises(ActionError):
            self.run_receive({"file_id": "f", "file_name": "a.bin", "size": str(limit + 1)}, transport)
        self.assertEqual(transport.calls, [])

    def test_size_within_the_limit_is_downloaded(self):
        transport = FakeTransport(b"hello")
        result = self.run_receive({"file_id": "f", "file_name": "a.bin", "size": "5"}, transport)
        self.assertTrue(result.ok, result.output)
        self.assertEqual(len(transport.calls), 1)

    def test_a_dangling_link_at_the_destination_is_refused(self):
        downloads = self.config.downloads_dir
        downloads.mkdir(parents=True, exist_ok=True)
        victim = self.outside / "victim.txt"
        _symlink(downloads / "report.txt", victim)  # dangling: victim does not exist yet
        transport = FakeTransport(b"payload")
        with self.assertRaises(ActionError):
            self.run_receive({"file_id": "f", "file_name": "report.txt", "size": "7"}, transport)
        self.assertFalse(victim.exists(), "the link target must not be created")

    def test_explicit_target_through_a_link_outside_the_roots_is_refused(self):
        self.config.allowed_roots = [self.ws]
        link = self.ws / "linked"
        _symlink(link, self.outside, directory=True)
        transport = FakeTransport(b"payload")
        with self.assertRaises(ActionError):
            self.run_receive({"file_id": "f", "size": "7", "target": str(link / "new.txt")}, transport)
        self.assertFalse((self.outside / "new.txt").exists())


class OnDocumentSizeTests(_Tmp):

    def test_document_without_a_reported_size_is_refused_without_a_plan(self):
        context = make_context(self.config)
        message = FakeMessage("")
        message.document = SimpleNamespace(file_size=None, file_name="a.bin", file_id="abcdefghijk")
        asyncio.run(botmod.on_document(make_update(message=message), context))
        self.assertEqual(context.application.bot_data["plans"], {})
        self.assertTrue(any("did not say how big" in r for r in message.replies), message.replies)

    def test_document_over_the_limit_is_refused_without_a_plan(self):
        context = make_context(self.config)
        message = FakeMessage("")
        message.document = SimpleNamespace(file_size=10**12, file_name="a.bin", file_id="abcdefghijk")
        asyncio.run(botmod.on_document(make_update(message=message), context))
        self.assertEqual(context.application.bot_data["plans"], {})


# ---------------------------------------------------------------------------
# D5: /pause and /resume
# ---------------------------------------------------------------------------

class PauseFileTests(IsolatedCase):

    def test_file_sits_beside_the_jobs_file(self):
        self.assertEqual(pause.pause_path(Path("/x/data/jobs.json")), Path("/x/data/pause.json"))

    def test_save_load_and_clear_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data" / "pause.json"
            self.assertIsNone(pause.load_pause(path))
            pause.save_pause(path, True, OWNER_ID)
            loaded = pause.load_pause(path)
            self.assertTrue(loaded["paused"])
            self.assertEqual(loaded["by"], OWNER_ID)
            self.assertFalse(path.with_name("pause.json.tmp").exists())
            pause.save_pause(path, False, None)
            self.assertIsNone(pause.load_pause(path))

    def test_a_damaged_file_keeps_the_bot_paused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pause.json"
            path.write_text("{not json", encoding="utf-8")
            loaded = pause.load_pause(path)
            self.assertTrue(loaded["paused"])
            self.assertTrue(loaded["damaged"])

    def test_exempt_commands_are_matched_exactly(self):
        for text in ("/resume", "/panic", "/status", "/resume@MyBot", "/status now", " /panic\n"):
            with self.subTest(text=text):
                self.assertTrue(pause.is_exempt(text))
        for text in ("/pause", "/statusx", "resume", "please /resume", "", None, "approve K7M2QP"):
            with self.subTest(text=text):
                self.assertFalse(pause.is_exempt(text))


class PauseGateTests(_Tmp):

    def setUp(self) -> None:
        super().setUp()
        self.pause_file = pause.pause_path(self.config.jobs_file)
        self.context = make_context(self.config)
        self.context.application.bot_data.update(paused=True, pause_file=self.pause_file)

    def gate(self, update):
        return asyncio.run(botmod.pause_gate(update, self.context))

    def test_owner_text_is_answered_and_stopped(self):
        message = FakeMessage("take a screenshot")
        with self.assertRaises(ApplicationHandlerStop):
            self.gate(make_update(message=message))
        self.assertTrue(any("Paused" in r for r in message.replies))

    def test_approval_text_is_stopped_so_nothing_is_approved(self):
        message = FakeMessage("approve K7M2QP")
        with self.assertRaises(ApplicationHandlerStop):
            self.gate(make_update(message=message))

    def test_resume_panic_and_status_pass_through(self):
        for text in ("/resume", "/panic", "/status"):
            with self.subTest(text=text):
                message = FakeMessage(text)
                self.gate(make_update(message=message))  # must not raise
                self.assertEqual(message.replies, [])

    def test_owner_button_press_is_answered_and_stopped(self):
        query = FakeQuery("apv:abc", OWNER_ID)
        with self.assertRaises(ApplicationHandlerStop):
            self.gate(make_update(query=query))
        self.assertEqual(query.answered, 1)

    def test_owner_document_is_stopped(self):
        message = FakeMessage("")
        message.document = SimpleNamespace(file_size=10, file_name="a.txt", file_id="abcdefghijk")
        with self.assertRaises(ApplicationHandlerStop):
            self.gate(make_update(message=message))

    def test_a_stranger_is_not_answered_here_and_owner_only_refuses_them(self):
        message = FakeMessage("hello")
        self.gate(make_update(message=message, user_id=OTHER_ID))  # no raise, no reply
        self.assertEqual(message.replies, [])

    def test_nothing_is_stopped_when_not_paused(self):
        self.context.application.bot_data["paused"] = False
        message = FakeMessage("hello")
        self.gate(make_update(message=message))
        self.assertEqual(message.replies, [])


class PauseCommandTests(_Tmp):

    def setUp(self) -> None:
        super().setUp()
        self.pause_file = pause.pause_path(self.config.jobs_file)
        self.context = make_context(self.config)
        self.context.application.bot_data.update(pause_file=self.pause_file, paused=False)

    def test_owner_pause_is_saved_and_reported(self):
        update = make_update()
        asyncio.run(botmod.cmd_pause(update, self.context))
        self.assertTrue(self.context.application.bot_data["paused"])
        self.assertTrue(self.pause_file.exists())
        self.assertTrue(any("Paused" in r for r in update.message.replies))

    def test_second_pause_says_so(self):
        asyncio.run(botmod.cmd_pause(make_update(), self.context))
        update = make_update()
        asyncio.run(botmod.cmd_pause(update, self.context))
        self.assertTrue(any("Already paused" in r for r in update.message.replies))

    def test_stranger_cannot_pause(self):
        update = make_update(user_id=OTHER_ID)
        asyncio.run(botmod.cmd_pause(update, self.context))
        self.assertFalse(self.context.application.bot_data["paused"])
        self.assertFalse(self.pause_file.exists())
        self.assertEqual(update.message.replies, [])

    def test_owner_resume_clears_the_pause(self):
        asyncio.run(botmod.cmd_pause(make_update(), self.context))
        update = make_update()
        asyncio.run(botmod.cmd_resume(update, self.context))
        self.assertFalse(self.context.application.bot_data["paused"])
        self.assertFalse(self.pause_file.exists())
        self.assertTrue(any("Resumed" in r for r in update.message.replies))

    def test_stranger_cannot_resume(self):
        asyncio.run(botmod.cmd_pause(make_update(), self.context))
        asyncio.run(botmod.cmd_resume(make_update(user_id=OTHER_ID), self.context))
        self.assertTrue(self.context.application.bot_data["paused"])
        self.assertTrue(self.pause_file.exists())

    def test_resume_when_not_paused_says_so(self):
        update = make_update()
        asyncio.run(botmod.cmd_resume(update, self.context))
        self.assertTrue(any("Not paused" in r for r in update.message.replies))

    def test_pause_stops_watch_and_remote_like_panic_does(self):
        class Watch:
            reasons: list[str] = []

            def stop_now(self, reason):
                self.reasons.append(reason)

        class Remote:
            reasons: list[str] = []

            async def stop(self, reason):
                self.reasons.append(reason)
                return True

        watch, remote = Watch(), Remote()
        self.context.application.bot_data.update(watch_controller=watch, remote_controller=remote)
        asyncio.run(botmod.cmd_pause(make_update(), self.context))
        self.assertEqual(watch.reasons, ["pause"])
        self.assertEqual(remote.reasons, ["pause"])

    def test_a_failed_save_still_pauses_in_memory_and_says_so(self):
        with mock.patch.object(botmod, "save_pause", side_effect=OSError("disk full")):
            update = make_update()
            asyncio.run(botmod.cmd_pause(update, self.context))
        self.assertTrue(self.context.application.bot_data["paused"])
        self.assertTrue(any("could NOT be saved" in r for r in update.message.replies))

    def test_clear_abort_does_not_clear_the_pause(self):
        asyncio.run(botmod.cmd_pause(make_update(), self.context))
        botmod.clear_abort()
        self.assertTrue(self.context.application.bot_data["paused"])

    def test_pause_survives_a_restart_and_resume_survives_too(self):
        asyncio.run(botmod.cmd_pause(make_update(), self.context))
        # python-telegram-bot checks the token format when the application is built.
        self.config.bot_token = "123456789:AAHrestartTestTokenabcdefghijklmnopqrst"
        app = botmod.build_application(self.config)       # a fresh process: reads the file
        self.assertTrue(app.bot_data["paused"])
        asyncio.run(botmod.cmd_resume(make_update(), self.context))
        app = botmod.build_application(self.config)
        self.assertFalse(app.bot_data["paused"])

    def test_status_shows_the_pause(self):
        asyncio.run(botmod.cmd_pause(make_update(), self.context))
        status_text = botmod.paused_status_line(self.context.application.bot_data)
        self.assertIn("PAUSED", status_text)
        self.assertIn("/resume", status_text)
        self.assertEqual(botmod.paused_status_line({"paused": False}), "▶️ Running")


class PauseDispatchTests(_Tmp):
    """Real PTB dispatch: the gate sits in front of every handler (group -1).

    Only the network is stubbed: getMe (initialize) and sendMessage.
    """

    def setUp(self) -> None:
        super().setUp()
        self.config.bot_token = "123456789:AAHrestartTestTokenabcdefghijklmnopqrst"
        self.sent: list[tuple[str, str]] = []

    def _run(self, scenario):
        from datetime import datetime

        from telegram import Chat, Message, MessageEntity, Update, User
        from telegram.ext import ExtBot

        app = botmod.build_application(self.config)
        pause_file = pause.pause_path(self.config.jobs_file)
        app.bot_data["pause_file"] = pause_file
        sent = self.sent

        class _Sent:
            message_id = 0

            async def edit_text(self, text, **_kwargs):
                sent.append(("edit", text))
                return self

            async def delete(self):
                return None

        async def fake_init(bot, *_a, **_k):
            bot._bot_user = User(id=123456789, first_name="tpa", is_bot=True, username="tpa_test_bot")

        async def fake_none(*_a, **_k):
            return None

        async def fake_send(bot, chat_id=None, text="", **_kwargs):
            sent.append((str(chat_id), text))
            return _Sent()

        def update(text: str, user_id: int, uid: int) -> Update:
            entities = ([MessageEntity("bot_command", 0, len(text.split()[0]))]
                        if text.startswith("/") else None)
            message = Message(message_id=uid, date=datetime.now(), chat=Chat(id=user_id, type="private"),
                              from_user=User(id=user_id, first_name="t", is_bot=False),
                              text=text, entities=entities)
            result = Update(update_id=uid, message=message)
            message.set_bot(app.bot)
            return result

        async def drive():
            with mock.patch.object(ExtBot, "initialize", fake_init), \
                    mock.patch.object(ExtBot, "shutdown", fake_none), \
                    mock.patch.object(ExtBot, "send_message", fake_send):
                await app.initialize()
                try:
                    await scenario(app, update)
                finally:
                    await app.shutdown()

        asyncio.run(drive())
        return app

    def test_paused_requests_are_answered_and_not_run(self):
        async def scenario(app, update):
            app.bot_data["paused"] = True
            await app.process_update(update("take a screenshot", OWNER_ID, 1))
            await app.process_update(update("approve ABCDEF", OWNER_ID, 2))
            await app.process_update(update("/pause", OWNER_ID, 3))
            await app.process_update(update("hello", OTHER_ID, 4))       # stranger: no reply
            self.assertEqual(app.bot_data["plans"], {}, "nothing may be planned while paused")

        self._run(scenario)
        replies = [text for _chat, text in self.sent]
        self.assertEqual(len(replies), 3, replies)
        self.assertTrue(all("Paused" in text for text in replies), replies)

    def test_status_and_resume_work_while_paused_and_resume_clears_it(self):
        async def scenario(app, update):
            app.bot_data["paused"] = True
            await app.process_update(update("/status", OWNER_ID, 1))
            await app.process_update(update("/resume", OWNER_ID, 2))
            self.assertFalse(app.bot_data["paused"])

        self._run(scenario)
        texts = [text for _chat, text in self.sent]
        self.assertTrue(any("PAUSED" in t for t in texts), texts)
        self.assertTrue(any("Resumed" in t for t in texts), texts)

    def test_pause_then_restart_is_still_paused_in_the_real_app(self):
        async def first(app, update):
            await app.process_update(update("/pause", OWNER_ID, 1))
            self.assertTrue(app.bot_data["paused"])

        self._run(first)
        self.assertTrue(pause.pause_path(self.config.jobs_file).exists())

        async def second(app, update):
            self.assertTrue(app.bot_data["paused"], "the restarted app must start paused")
            await app.process_update(update("hello", OWNER_ID, 2))

        self._run(second)
        self.assertTrue(any("Paused" in text for _chat, text in self.sent))


class PausedBriefingTests(_Tmp):

    def test_due_briefing_is_not_queued_while_paused(self):
        app = SimpleNamespace(bot_data={"config": self.config, "plans": {}, "paused": True},
                              bot=FakeBot())
        job = SimpleNamespace(chat_id=OWNER_ID, kind="reminder", text="call", command="")
        queued = asyncio.run(botmod.queue_briefing(app, job))
        self.assertFalse(queued)
        self.assertEqual(app.bot_data["plans"], {})


# ---------------------------------------------------------------------------
# D6: refuse to run as Administrator
# ---------------------------------------------------------------------------

class ElevationCheckTests(IsolatedCase):

    def test_not_windows_is_never_elevated(self):
        self.assertFalse(elevation.is_administrator(is_windows=False, check=lambda: True))

    def test_windows_answer_comes_from_the_check(self):
        self.assertTrue(elevation.is_administrator(is_windows=True, check=lambda: True))
        self.assertFalse(elevation.is_administrator(is_windows=True, check=lambda: False))

    def test_unknown_answer_is_not_treated_as_elevated(self):
        self.assertIsNone(elevation.is_administrator(is_windows=True, check=lambda: None))

    def test_the_refusal_text_contains_no_override_hint(self):
        self.assertNotIn("--", elevation.REFUSAL_TEXT)


class ElevatedStartTests(_Tmp):

    def run_main(self, argv, elevated):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(elevation, "is_administrator", return_value=elevated), \
                redirect_stdout(out), redirect_stderr(err):
            code = main_mod.main(argv)
        return code, out.getvalue(), err.getvalue()

    def config_args(self):
        return ["--config", str(self.config.config_path)]

    def test_elevated_start_is_refused_with_exit_5_and_no_polling(self):
        with mock.patch.object(main_mod, "_notify_owners", return_value=True) as notify, \
                mock.patch.object(main_mod, "run_polling_with_retries") as polling:
            code, _out, err = self.run_main(self.config_args(), elevated=True)
        self.assertEqual(code, elevation.ELEVATED_EXIT_CODE)
        self.assertIn("refused", err)
        polling.assert_not_called()
        notify.assert_called_once()
        self.assertIn("Administrator", notify.call_args.args[1])

    def test_elevated_set_token_and_forget_token_are_refused(self):
        for flag in ("--set-token", "--forget-token"):
            with self.subTest(flag=flag), mock.patch.object(main_mod, "cmd_set_token") as setter, \
                    mock.patch.object(main_mod, "cmd_forget_token") as forgetter, \
                    mock.patch.object(main_mod, "_notify_owners", return_value=False):
                code, _o, _e = self.run_main([flag, *self.config_args()], elevated=True)
                self.assertEqual(code, elevation.ELEVATED_EXIT_CODE)
                setter.assert_not_called()
                forgetter.assert_not_called()

    def test_check_still_runs_when_elevated_and_reports_it(self):
        with mock.patch.object(main_mod, "self_check", return_value=1) as check:
            code, _o, _e = self.run_main(["--check", *self.config_args()], elevated=True)
        check.assert_called_once()
        self.assertEqual(code, 1)

    def test_check_when_not_elevated_runs_the_normal_check(self):
        with mock.patch.object(main_mod, "self_check", return_value=0) as check:
            code, _o, _e = self.run_main(["--check", *self.config_args()], elevated=False)
        check.assert_called_once()
        self.assertEqual(code, 0)

    def test_self_check_reports_elevation_as_a_problem(self):
        # No planner endpoint: self_check would otherwise try the configured cloud URL.
        no_planner = SimpleNamespace(status=lambda: {})
        with mock.patch.object(elevation, "is_administrator", return_value=True), \
                mock.patch.object(main_mod, "build_planner", return_value=no_planner), \
                mock.patch.object(main_mod.token_store, "validate_token",
                                  return_value=(True, "stubbed", None)):
            out = io.StringIO()
            with redirect_stdout(out):
                code = main_mod.self_check(self.config)
        self.assertIn("administrator", out.getvalue())
        self.assertNotEqual(code, 0)

    def test_no_override_flag_exists(self):
        with mock.patch.object(elevation, "is_administrator", return_value=True), \
                redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main_mod.main(["--allow-admin", *self.config_args()])

    def test_refusal_still_exits_when_the_config_cannot_load(self):
        with mock.patch.object(main_mod, "_notify_owners") as notify:
            code, _o, _e = self.run_main(["--config", str(self.dir / "missing.ini")], elevated=True)
        self.assertEqual(code, elevation.ELEVATED_EXIT_CODE)
        notify.assert_not_called()


# ---------------------------------------------------------------------------
# Windows ACL command shape (runs on Linux: the subprocess is stubbed)
# ---------------------------------------------------------------------------

class AclCommandShapeTests(IsolatedCase):

    SID = "S-1-5-21-111-222-333-1001"

    def test_path_travels_in_the_environment_never_in_the_script(self):
        odd = Path(r"C:\Users\me\مجلد الاختبار\store with spaces\token.json")
        seen = {}

        def fake_run(args, **kwargs):
            seen["args"] = args
            seen["env"] = kwargs["env"]
            return SimpleNamespace(returncode=0, stdout='{"owner": "%s", "entries": []}' % self.SID)

        with mock.patch.object(token_store.subprocess, "run", side_effect=fake_run):
            token_store._run_acl_script(token_store._ACL_APPLY_SCRIPT, odd, self.SID, directory=False)

        encoded = seen["args"][-1]
        script = base64.b64decode(encoded).decode("utf-16-le")
        self.assertNotIn(str(odd), script)
        self.assertNotIn(self.SID, script)
        self.assertEqual(seen["env"]["TPA_ACL_PATH"], str(odd))
        self.assertEqual(seen["env"]["TPA_ACL_SID"], self.SID)
        # Round 9: the executable is the full Windows PowerShell path (or the bare name), never a PS7 module setup.
        self.assertTrue(seen["args"][0] == "powershell" or seen["args"][0].lower().endswith("powershell.exe"))
        self.assertEqual(seen["args"][1:4], ["-NoProfile", "-NonInteractive", "-EncodedCommand"])
        self.assertNotIn("PSModulePath", seen["env"])

    def test_script_turns_inheritance_off_and_removes_every_entry(self):
        script = token_store._ACL_APPLY_SCRIPT
        self.assertIn("SetAccessRuleProtection($true, $false)", script)
        self.assertIn("RemoveAccessRule", script)
        self.assertIn("SetOwner($sid)", script)
        self.assertNotIn("icacls", script.lower())

    def test_failed_process_is_not_success(self):
        with mock.patch.object(token_store.subprocess, "run",
                               return_value=SimpleNamespace(returncode=1, stdout="")):
            self.assertIsNone(token_store._run_acl_script(token_store._ACL_READ_SCRIPT,
                                                          Path("x"), self.SID, directory=False))

    def test_missing_powershell_is_not_success(self):
        with mock.patch.object(token_store.subprocess, "run", side_effect=OSError("no powershell")):
            self.assertIsNone(token_store._run_acl_script(token_store._ACL_READ_SCRIPT,
                                                          Path("x"), self.SID, directory=False))


class AclOwnerOnlyPredicateTests(IsolatedCase):
    """The read-back decision: only the owner, as the one explicit entry. Closes mutation M8."""

    SID = "S-1-5-21-1-2-3-1001"
    OTHER = "S-1-5-18"  # SYSTEM

    def owner_only(self, **overrides):
        report = {"owner": self.SID, "entries": [{"sid": self.SID, "inherited": False}]}
        report.update(overrides)
        return report

    def test_owner_only_report_passes(self):
        self.assertTrue(token_store.acl_is_owner_only(self.owner_only(), self.SID))

    def test_an_extra_explicit_entry_fails(self):
        report = self.owner_only(entries=[{"sid": self.SID, "inherited": False},
                                          {"sid": self.OTHER, "inherited": False}])
        self.assertFalse(token_store.acl_is_owner_only(report, self.SID))

    def test_only_another_account_fails(self):
        report = self.owner_only(entries=[{"sid": self.OTHER, "inherited": False}])
        self.assertFalse(token_store.acl_is_owner_only(report, self.SID))

    def test_an_inherited_entry_fails_even_for_the_owner(self):
        report = self.owner_only(entries=[{"sid": self.SID, "inherited": True}])
        self.assertFalse(token_store.acl_is_owner_only(report, self.SID))

    def test_an_inherited_flag_that_is_missing_or_not_false_fails(self):
        for entry in ({"sid": self.SID}, {"sid": self.SID, "inherited": None},
                      {"sid": self.SID, "inherited": 0}):
            with self.subTest(entry=entry):
                self.assertFalse(token_store.acl_is_owner_only(self.owner_only(entries=[entry]), self.SID))

    def test_a_different_owner_fails(self):
        self.assertFalse(token_store.acl_is_owner_only(self.owner_only(owner=self.OTHER), self.SID))

    def test_an_empty_list_fails_so_a_missing_report_never_passes(self):
        self.assertFalse(token_store.acl_is_owner_only(self.owner_only(entries=[]), self.SID))
        self.assertFalse(token_store.acl_is_owner_only({"owner": self.SID}, self.SID))

    def test_missing_report_or_sid_fails(self):
        self.assertFalse(token_store.acl_is_owner_only(None, self.SID))
        self.assertFalse(token_store.acl_is_owner_only("not a report", self.SID))
        self.assertFalse(token_store.acl_is_owner_only(self.owner_only(), ""))
        self.assertFalse(token_store.acl_is_owner_only(self.owner_only(), None))


if __name__ == "__main__":
    unittest.main()
