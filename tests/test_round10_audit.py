"""Round 10 audit: every Telegram command end to end, with a stubbed Telegram HTTP layer.

Only the HTTP transport is replaced (``HTTPXRequest.do_request``). Real python-telegram-bot
objects, real dispatch, the pause gate, the redacting wrapper and the approval code all run.
Windows calls (winget, PowerShell, the health probes, disk usage) are stubbed. Nothing here
reaches the network, starts a process, or touches the real token store.
"""
from __future__ import annotations

import asyncio
import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_flow import OTHER_ID, OWNER_ID, make_config  # noqa: E402  (shared fixtures)
from test_round9_features import _healthy_ps  # noqa: E402  (the same Windows probe answers)

from _hermetic import IsolatedCase, ModuleGuard, release_log_then_remove  # noqa: E402

from telegram import CallbackQuery, Chat, Document, Message, MessageEntity, Update, User  # noqa: E402
from telegram.error import NetworkError  # noqa: E402
from telegram.ext import CommandHandler  # noqa: E402
from telegram.request import HTTPXRequest  # noqa: E402

from assistant import audit, health, pause  # noqa: E402
from assistant import bot as botmod  # noqa: E402
from assistant.actions import apps  # noqa: E402
from assistant.config import ConfigError, load_config  # noqa: E402

# A token in the real format (PTB refuses shorter fake ones). It is only ever used against the fake transport.
TOKEN = "123456789:AAHrestartTestTokenabcdefghijklmnopqrst"
SECRET_PART = "AAHrestartTestTokenabcdefghijklmnopqrst"
WINGET = "C:\\Windows\\System32\\winget.exe"
SENDING_METHODS = {"sendMessage", "editMessageText", "sendDocument", "sendPhoto", "sendChatAction"}

_guard = ModuleGuard()


def setUpModule():
    _guard.start()


def tearDownModule():
    _guard.stop()


def command_names(app) -> list[str]:
    """Every command the bot registers, read from the real dispatcher (no hand-kept list)."""
    names: set[str] = set()
    for handler in app.handlers[0]:
        if isinstance(handler, CommandHandler):
            names |= set(handler.commands)
    return sorted(names)


class _NoPlanner:
    """Stands in for the planner: any call means a request reached planning."""

    def plan(self, *_args, **_kwargs):
        raise AssertionError("the planner was called")


class Session:
    """One running Application, driven with real updates."""

    def __init__(self, case: "WireBase", app) -> None:
        self.case = case
        self.app = app

    def plans(self) -> dict:
        return self.app.bot_data["plans"]

    def code_for(self, action: str) -> str:
        matches = [p.code for p in self.plans().values() if p.plan.steps[0].action == action]
        assert len(matches) == 1, f"expected one pending {action} plan, found {len(matches)}"
        return matches[0]

    def plan_id_for(self, action: str) -> str:
        matches = [k for k, p in self.plans().items() if p.plan.steps[0].action == action]
        assert len(matches) == 1, matches
        return matches[0]

    def _next_id(self) -> int:
        self.case.next_id += 1
        return self.case.next_id

    def _message(self, text, user, chat_type="private", document=None) -> Update:
        mid = self._next_id()
        entities = None
        if text and text.startswith("/") and text.split():
            entities = [MessageEntity("bot_command", 0, len(text.split()[0]))]
        chat_id = user if chat_type == "private" else -1001
        message = Message(message_id=mid, date=datetime.now(), chat=Chat(id=chat_id, type=chat_type),
                          from_user=User(id=user, first_name="t", is_bot=False),
                          text=text, entities=entities, document=document)
        message.set_bot(self.app.bot)
        return Update(update_id=mid, message=message)

    async def say(self, text, user=OWNER_ID, chat_type="private") -> None:
        await self.app.process_update(self._message(text, user, chat_type))

    async def upload(self, name, size, user=OWNER_ID) -> None:
        document = Document(file_id="FILE1", file_unique_id="UNIQ", file_name=name, file_size=size)
        await self.app.process_update(self._message(None, user, document=document))

    async def press(self, data, user=OWNER_ID) -> None:
        mid = self._next_id()
        origin = Message(message_id=mid, date=datetime.now(), chat=Chat(id=user, type="private"),
                         text="plan", from_user=User(id=1, first_name="bot", is_bot=True))
        origin.set_bot(self.app.bot)
        query = CallbackQuery(id=str(mid), from_user=User(id=user, first_name="t", is_bot=False),
                              chat_instance="ci", data=data, message=origin)
        query.set_bot(self.app.bot)
        await self.app.process_update(Update(update_id=mid, callback_query=query))


class WireBase(IsolatedCase):
    """Runs real PTB dispatch against a fake Telegram HTTP layer and fake Windows calls."""

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(release_log_then_remove, self._tmp)
        self.dir = Path(self._tmp.name)
        self.config = make_config(self.dir)
        self.config.bot_token = TOKEN
        audit.register_secret(TOKEN)
        audit.setup(self.dir / "logs" / "a.log", "DEBUG")
        self.calls: list[tuple[str, dict]] = []
        self.scripted: dict[str, list] = {}
        self.next_id = 500
        self.winget_calls: list[list[str]] = []
        self.winget_exit = 0
        self.ps_calls: list[str] = []
        self.enterContext(mock.patch.object(HTTPXRequest, "do_request", new=self._fake_do_request))
        # bot.py imports winget_path by name, so both references are stubbed.
        self.enterContext(mock.patch.object(apps, "winget_path", lambda: WINGET))
        self.enterContext(mock.patch.object(botmod, "winget_path", lambda: WINGET))
        self.enterContext(mock.patch.object(apps, "run_process", self._fake_winget))
        real_run_health = health.run_health
        self.enterContext(mock.patch.object(
            health, "run_health", lambda **kw: real_run_health(**{"windows": True, **kw})))
        self.enterContext(mock.patch.object(health, "_ps_json", self._fake_ps))
        self.enterContext(mock.patch.object(health, "run_powershell", self._no_powershell))
        self.enterContext(mock.patch.object(health, "_fixed_drive_letters", lambda: ["C"]))
        self.enterContext(mock.patch("shutil.disk_usage", lambda path: SimpleNamespace(
            free=int(120 * health.GIB), total=int(200 * health.GIB))))

    # ----- stubs -------------------------------------------------------------------------------
    async def _fake_do_request(self, url, method, request_data=None, **_kwargs):
        name = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters or {}) if request_data is not None else {}
        self.calls.append((name, params))
        queue = self.scripted.get(name)
        if queue:
            outcome = queue.pop(0)
            if not queue:
                del self.scripted[name]
            if outcome == "network":
                raise NetworkError("simulated network drop")
            if isinstance(outcome, int):             # HTTP 429 with Telegram's retry time
                body = {"ok": False, "error_code": 429,
                        "description": f"Too Many Requests: retry after {outcome}",
                        "parameters": {"retry_after": outcome}}
                return 429, json.dumps(body).encode("utf-8")
        result = self._result(name, params)
        return 200, json.dumps({"ok": True, "result": result}).encode("utf-8")

    def _result(self, name, params):
        if name == "getMe":
            return {"id": 123456789, "is_bot": True, "first_name": "tpa", "username": "tpa_test_bot"}
        if name in SENDING_METHODS - {"sendChatAction"}:
            self.next_id += 1
            chat_id = params.get("chat_id") or 0
            return {"message_id": self.next_id, "date": 1700000000,
                    "chat": {"id": int(chat_id), "type": "private"}, "text": params.get("text", "")}
        if name == "getFile":
            return {"file_id": params.get("file_id", "f"), "file_unique_id": "u", "file_size": 5,
                    "file_path": "documents/file.bin"}
        return True

    def _fake_winget(self, argv, timeout=None, cwd=None, **_kwargs):
        self.winget_calls.append(list(argv))
        return SimpleNamespace(returncode=self.winget_exit, stdout="winget output", stderr="")

    def _fake_ps(self, script, what):
        self.ps_calls.append(what)
        return _healthy_ps(script, what)

    def _no_powershell(self, *_args, **_kwargs):
        raise AssertionError("PowerShell must not run here: the health probes are stubbed")

    # ----- driving -----------------------------------------------------------------------------
    def session(self, scenario) -> None:
        async def drive():
            app = botmod.build_application(self.config)
            await app.initialize()
            try:
                await scenario(Session(self, app))
            finally:
                await app.shutdown()

        asyncio.run(drive())

    # ----- what Telegram would have received ---------------------------------------------------
    def method_names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def sent_texts(self) -> list[str]:
        return [p.get("text", "") for name, p in self.calls if name == "sendMessage"]

    def all_outbound(self) -> str:
        return repr(self.calls)

    def log_text(self) -> str:
        audit.close()
        path = self.dir / "logs" / "a.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""


# ==========================================================================================
# 1. Who is served, and what each command does
# ==========================================================================================

class AccessTests(WireBase):

    def test_stranger_gets_no_reply_from_any_command_text_upload_or_group(self):
        async def scenario(s):
            names = command_names(s.app)
            self.assertGreater(len(names), 30)
            for name in names:
                await s.say(f"/{name} K7M2QP", user=OTHER_ID)
            await s.say("take a screenshot", user=OTHER_ID)
            await s.upload("evil.exe", 10, user=OTHER_ID)
            await s.press("apv:nothing", user=OTHER_ID)
            await s.say("/status", user=OWNER_ID, chat_type="supergroup")
            self.assertEqual(s.plans(), {})

        self.session(scenario)
        self.assertEqual(self.sent_texts(), [], "a stranger or a group must get no reply")
        self.assertFalse({"sendMessage", "editMessageText", "sendDocument"} & set(self.method_names()))
        self.assertEqual(self.winget_calls, [])
        self.assertIn("unauthorised user", self.log_text())

    def test_owner_read_only_commands_all_answer_without_an_internal_error(self):
        async def scenario(s):
            for command in ("/start", "/help", "/id", "/whoami", "/status", "/privacy", "/jobs",
                            "/logs", "/notes", "/macros", "/language", "/health"):
                await s.say(command)

        self.session(scenario)
        replies = self.sent_texts()
        self.assertGreaterEqual(len(replies), 12)
        self.assertFalse(any("Internal error" in r for r in replies), replies)
        self.assertEqual(self.winget_calls, [])

    def test_bot_token_never_appears_in_any_reply_or_log(self):
        async def scenario(s):
            s.app.bot_data["planner"] = _NoPlanner()
            await s.say(TOKEN)
            await s.say(f"/install {TOKEN}")
            await s.say(f"take a screenshot of {TOKEN}")
            await s.say("/status")
            await s.say("/logs")

        self.session(scenario)
        self.assertNotIn(SECRET_PART, self.all_outbound())
        self.assertNotIn(SECRET_PART, self.log_text())
        self.assertNotIn(TOKEN, self.log_text())

    def test_pasted_token_is_refused_before_any_planner_sees_it(self):
        other_shape = "999999999:" + "A" * 35

        async def scenario(s):
            s.app.bot_data["planner"] = _NoPlanner()
            for text in (TOKEN, other_shape, f"please use {TOKEN} to log in"):
                await s.say(text)
            self.assertEqual(s.plans(), {})

        self.session(scenario)
        replies = self.sent_texts()
        self.assertEqual(len(replies), 3, replies)
        self.assertTrue(all("looks like a Telegram bot token" in r for r in replies), replies)
        self.assertNotIn(SECRET_PART, self.all_outbound())

    def test_group_chat_messages_are_ignored_even_for_the_owner(self):
        async def scenario(s):
            await s.say("/help", chat_type="group")
            await s.say("take a screenshot", chat_type="supergroup")
            self.assertEqual(s.plans(), {})

        self.session(scenario)
        self.assertEqual(self.sent_texts(), [])


class PauseTests(WireBase):

    def test_paused_bot_refuses_every_command_except_resume_panic_and_status(self):
        async def scenario(s):
            await s.say("/pause")
            self.assertTrue(pause.pause_path(self.config.jobs_file).exists())
            exempt = {"resume", "panic", "status"}
            for name in command_names(s.app):
                if name in exempt:
                    continue
                before = len(self.sent_texts())
                await s.say(f"/{name} x")
                new = self.sent_texts()[before:]
                self.assertEqual(new, [botmod.PAUSED_NOTICE], f"/{name} must be refused while paused")
            await s.say("take a screenshot")
            await s.say("approve K7M2QP")
            self.assertEqual(s.plans(), {}, "nothing may be planned while paused")

        self.session(scenario)
        self.assertEqual(self.winget_calls, [])

    def test_status_still_answers_while_paused_and_resume_clears_the_pause(self):
        async def scenario(s):
            await s.say("/pause")
            await s.say("/status")
            await s.say("/resume")
            self.assertFalse(s.app.bot_data["paused"])

        self.session(scenario)
        texts = self.sent_texts()
        self.assertTrue(any("PAUSED" in t for t in texts), texts)
        self.assertTrue(any("Resumed" in t for t in texts), texts)
        self.assertFalse(pause.pause_path(self.config.jobs_file).exists())

    def test_paused_approval_does_nothing_and_the_plan_survives_until_resume(self):
        async def first(s):
            await s.say("/install vlc")
            code = s.code_for("app_install")
            plan_id = s.plan_id_for("app_install")
            await s.say("/pause")
            await s.say(f"approve {code}")
            await s.press(f"apv:{plan_id}")
            self.assertEqual(self.winget_calls, [], "a paused bot must not run winget")
            self.assertIn(plan_id, s.plans(), "a refused approval must leave the plan in place")
            await s.say("/resume")
            await s.press(f"apv:{plan_id}")

        self.session(first)
        self.assertEqual(len(self.winget_calls), 1)
        self.assertEqual(self.winget_calls[0][1:3], ["install", "--exact"])

    def test_pause_survives_a_restart(self):
        self.session(lambda s: s.say("/pause"))

        async def second(s):
            self.assertTrue(s.app.bot_data["paused"], "the restarted app must start paused")
            await s.say("/install vlc")
            self.assertEqual(s.plans(), {})

        self.session(second)
        self.assertEqual(self.winget_calls, [])
        self.assertTrue(any(botmod.PAUSED_NOTICE == t for t in self.sent_texts()))


# ==========================================================================================
# 2. /install and /uninstall (winget)
# ==========================================================================================

INSTALL_ARGS = ["install", "--exact", "--id", "VideoLAN.VLC", "--source", "winget",
                "--accept-package-agreements", "--accept-source-agreements"]
UNINSTALL_ARGS = ["uninstall", "--exact", "--id", "VideoLAN.VLC", "--source", "winget",
                  "--accept-source-agreements"]


class AppApprovalTests(WireBase):

    def test_unlisted_name_is_refused_and_the_message_names_the_allow_list(self):
        async def scenario(s):
            await s.say("/install evil")
            self.assertEqual(s.plans(), {})
            text = self.sent_texts()[-1]
            self.assertIn("allow-list", text)
            self.assertIn("extra_allowed_apps", text)

        self.session(scenario)
        self.assertEqual(self.winget_calls, [])

    def test_install_plan_is_high_risk_with_the_exact_id_and_a_uac_warning(self):
        async def scenario(s):
            await s.say("/install vlc")
            self.assertEqual(len(s.plans()), 1)
            self.assertEqual(s.plans()[s.plan_id_for("app_install")].plan.risk, "high")

        self.session(scenario)
        text = " ".join(self.sent_texts())
        self.assertIn("VideoLAN.VLC", text)
        self.assertIn("UAC", text)
        self.assertIn("accept", text.lower())
        self.assertEqual(self.winget_calls, [])

    def test_approve_by_code_runs_the_exact_winget_argument_list_once(self):
        async def scenario(s):
            await s.say("/install vlc")
            code = s.code_for("app_install")
            await s.say(f"/yes {code}")

        self.session(scenario)
        self.assertEqual(len(self.winget_calls), 1)
        self.assertEqual(self.winget_calls[0][1:], INSTALL_ARGS)
        self.assertIn("Done", " ".join(self.sent_texts()))

    def test_deny_by_code_never_runs_winget(self):
        async def scenario(s):
            await s.say("/install vlc")
            code = s.code_for("app_install")
            await s.say(f"/no {code}")
            self.assertEqual(s.plans(), {})

        self.session(scenario)
        self.assertEqual(self.winget_calls, [])
        self.assertIn("Denied", " ".join(self.sent_texts()) + " " + " ".join(
            p.get("text", "") for n, p in self.calls if n == "editMessageText"))

    def test_button_approve_runs_and_button_deny_does_not(self):
        async def scenario(s):
            await s.say("/install vlc")
            deny_id = s.plan_id_for("app_install")
            await s.press(f"deny:{deny_id}")
            await s.say("/install vlc")
            approve_id = s.plan_id_for("app_install")
            await s.press(f"apv:{approve_id}")

        self.session(scenario)
        self.assertEqual(len(self.winget_calls), 1, "only the approved plan may run")
        self.assertEqual(self.winget_calls[0][1:], INSTALL_ARGS)

    def test_a_non_zero_winget_exit_code_is_never_reported_as_success(self):
        self.winget_exit = 1603

        async def scenario(s):
            await s.say("/install vlc")
            await s.say(f"/yes {s.code_for('app_install')}")

        self.session(scenario)
        text = " ".join(self.sent_texts())
        self.assertEqual(len(self.winget_calls), 1)
        self.assertIn("Finished with errors", text)
        self.assertNotIn("✅ Done", text)

    def test_an_expired_code_is_refused_and_winget_does_not_run(self):
        async def scenario(s):
            await s.say("/install vlc")
            code = s.code_for("app_install")
            for pending in s.plans().values():
                pending.created_at -= 16 * 60
            await s.say(f"/yes {code}")
            self.assertEqual(s.plans(), {})

        self.session(scenario)
        self.assertEqual(self.winget_calls, [])
        self.assertIn("No live plan", " ".join(self.sent_texts()))

    def test_an_expired_button_is_refused_and_winget_does_not_run(self):
        async def scenario(s):
            await s.say("/install vlc")
            plan_id = s.plan_id_for("app_install")
            for pending in s.plans().values():
                pending.created_at -= timedelta(minutes=16).total_seconds()
            await s.press(f"apv:{plan_id}")

        self.session(scenario)
        self.assertEqual(self.winget_calls, [])
        edits = [p.get("text", "") for n, p in self.calls if n == "editMessageText"]
        self.assertTrue(any("expired" in t for t in edits), edits)

    def test_a_restart_mid_approval_never_runs_the_plan(self):
        async def first(s):
            await s.say("/install vlc")
            self.code = s.code_for("app_install")

        self.session(first)
        self.assertEqual(self.winget_calls, [])

        async def second(s):
            self.assertEqual(s.plans(), {}, "pending plans are not restored after a restart")
            await s.say(f"/yes {self.code}")

        self.session(second)
        self.assertEqual(self.winget_calls, [], "a plan from before the restart must not run")
        self.assertIn("No live plan", " ".join(self.sent_texts()))

    def test_a_double_approval_runs_winget_once(self):
        async def scenario(s):
            await s.say("/install vlc")
            code = s.code_for("app_install")
            await s.say(f"/yes {code}")
            await s.say(f"/yes {code}")

        self.session(scenario)
        self.assertEqual(len(self.winget_calls), 1)

    def test_panic_cancels_a_pending_install(self):
        async def scenario(s):
            await s.say("/install vlc")
            code = s.code_for("app_install")
            await s.say("/panic")
            self.assertEqual(s.plans(), {})
            await s.say(f"/yes {code}")

        self.session(scenario)
        self.assertEqual(self.winget_calls, [])

    def test_uninstall_uses_its_own_argument_list_without_package_agreements(self):
        async def scenario(s):
            await s.say("/uninstall vlc")
            await s.say(f"/yes {s.code_for('app_uninstall')}")

        self.session(scenario)
        self.assertEqual(len(self.winget_calls), 1)
        self.assertEqual(self.winget_calls[0][1:], UNINSTALL_ARGS)
        self.assertNotIn("--accept-package-agreements", self.winget_calls[0])

    def test_missing_winget_is_said_and_no_plan_is_made(self):
        with mock.patch.object(botmod, "winget_path", lambda: None):
            async def scenario(s):
                await s.say("/install vlc")
                self.assertEqual(s.plans(), {})

            self.session(scenario)
        self.assertEqual(self.winget_calls, [])
        self.assertIn("winget is not available", " ".join(self.sent_texts()))


# ==========================================================================================
# 3. /health (read only, no approval)
# ==========================================================================================

class HealthCommandTests(WireBase):

    def test_health_is_read_only_and_creates_no_plan(self):
        async def scenario(s):
            await s.say("/health")
            self.assertEqual(s.plans(), {})

        self.session(scenario)
        self.assertTrue(self.ps_calls, "the stubbed probes were not used")
        self.assertEqual(self.winget_calls, [])
        text = " ".join(self.sent_texts())
        self.assertIn("PC health check", text)

    def test_health_output_has_no_home_path_or_user_name(self):
        async def scenario(s):
            await s.say("/health")

        self.session(scenario)
        text = " ".join(self.sent_texts())
        self.assertNotIn(str(Path.home()), text)
        self.assertNotIn("C:\\Users\\", text)


# ==========================================================================================
# 4. /remote and file transfers (approval gates)
# ==========================================================================================

class RemoteAndFileTests(WireBase):

    def test_remote_start_needs_approval_and_a_denial_starts_nothing(self):
        with mock.patch.object(botmod, "_run_remote_start",
                               side_effect=AssertionError("remote started")) as started:
            async def scenario(s):
                # The platform check is a designed seam: pretend this is Windows so the approval gate runs.
                s.app.bot_data["remote_platform_ok"] = lambda: True
                await s.say("/remote start 5")
                self.assertEqual(len(s.plans()), 1)
                await s.say(f"/no {s.code_for('remote_start')}")
                self.assertEqual(s.plans(), {})

            self.session(scenario)
        started.assert_not_called()
        self.assertFalse(any("Internal error" in t for t in self.sent_texts()))

    def test_upload_over_the_size_limit_is_refused_before_any_download(self):
        async def scenario(s):
            await s.upload("big.bin", 500 * 1024 * 1024)

        self.session(scenario)
        self.assertNotIn("getFile", self.method_names())
        self.assertTrue(any("MB" in t for t in self.sent_texts()), self.sent_texts())

    def test_upload_without_a_declared_size_is_refused_before_any_download(self):
        async def scenario(s):
            await s.upload("unknown.bin", None)

        self.session(scenario)
        self.assertNotIn("getFile", self.method_names())
        self.assertTrue(any("did not say how big" in t for t in self.sent_texts()))

    def test_saved_upload_names_cannot_escape_the_downloads_folder(self):
        from assistant.actions.base import sanitize_filename
        for raw in ("../../evil.txt", "..\\..\\evil.txt", "C:\\Windows\\x.exe", "a/b:c*?.txt", "   "):
            name = sanitize_filename(raw)
            self.assertNotIn("/", name, raw)
            self.assertNotIn("\\", name, raw)
            self.assertNotIn("..", name, raw)
            self.assertTrue(name, raw)


# ==========================================================================================
# 5. Telegram transport: rate limits, network drops, long and emoji text, secrets
# ==========================================================================================

class TransportTests(WireBase):

    def test_rate_limited_reply_is_resent_after_telegram_retry_time(self):
        self.scripted["sendMessage"] = [1]              # first send: 429, retry after 1 s

        async def scenario(s):
            await s.say("/help")

        self.session(scenario)
        # Attempt 1 gets 429; attempt 2 (after the wait) succeeds. Exactly two attempts.
        attempts = [t for t in self.sent_texts() if t == botmod.MSG["help"]]
        self.assertEqual(len(attempts), 2, "one 429 then one resend, no more")
        self.assertFalse(any("Internal error" in t for t in self.sent_texts()))

    def test_a_transient_network_drop_is_retried_and_delivered(self):
        self.scripted["sendMessage"] = ["network"]

        async def scenario(s):
            await s.say("/help")

        self.session(scenario)
        attempts = [t for t in self.sent_texts() if t == botmod.MSG["help"]]
        self.assertEqual(len(attempts), 2, "one dropped connection then one resend, no more")

    def test_emoji_heavy_output_is_split_within_telegram_limit(self):
        self.assertLessEqual(botmod.utf16_len("🛑" * 3000), 6000)   # the input really is too long
        chunks = botmod.split_message("🛑" * 3000)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(botmod.utf16_len(chunk), botmod.MAX_TG_LEN, "a chunk exceeds Telegram's limit")
        self.assertEqual("".join(chunks), "🛑" * 3000)

    def test_arabic_text_is_split_intact(self):
        text = ("مرحبا بالعالم " * 400).strip()
        chunks = botmod.split_message(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(botmod.utf16_len(c) <= botmod.MAX_TG_LEN for c in chunks))
        self.assertEqual("".join(chunks), text, "no character may be lost or added at a split")

    def test_a_secret_straddling_a_chunk_boundary_is_removed_from_every_chunk(self):
        sent = []

        class RecordingBot:
            async def send_message(self, chat_id, text, **_kwargs):
                sent.append(text)

        text = "a" * (botmod.MAX_TG_LEN - 10) + TOKEN + " tail"
        asyncio.run(botmod.send_long(RecordingBot(), 1, text))
        self.assertGreaterEqual(len(sent), 2, "the test needs the token to cross a boundary")
        joined = "".join(sent)
        self.assertNotIn(SECRET_PART[:12], joined)
        self.assertNotIn(SECRET_PART[-12:], joined)
        self.assertNotIn("AAHrestart", joined)


# ==========================================================================================
# 6. Configuration: bad and missing values fail with a clear message, never with the token
# ==========================================================================================

class ConfigErrorTests(IsolatedCase):

    def _load(self, body: str):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "config.ini"
        path.write_text(body, encoding="utf-8")
        return load_config(path)

    def test_non_numeric_user_id_is_a_clear_error(self):
        with self.assertRaisesRegex(ConfigError, "allowed_user_ids contains a non-numeric value"):
            self._load("[telegram]\nallowed_user_ids = abc\n")

    def test_empty_user_list_is_refused(self):
        with self.assertRaisesRegex(ConfigError, "allowed_user_ids is empty"):
            self._load("[telegram]\nallowed_user_ids =\n")

    def test_malformed_extra_app_is_refused_at_start(self):
        with self.assertRaisesRegex(ConfigError, r"\[apps\] extra_allowed_apps"):
            self._load("[telegram]\nallowed_user_ids = 42\n[apps]\nextra_allowed_apps = obsidian\n")

    def test_non_numeric_timeout_is_refused(self):
        with self.assertRaisesRegex(ConfigError, "command_timeout must be a number"):
            self._load("[telegram]\nallowed_user_ids = 42\n[behaviour]\ncommand_timeout = soon\n")

    def test_unknown_planner_mode_is_refused(self):
        with self.assertRaisesRegex(ConfigError, "mode must be one of"):
            self._load("[telegram]\nallowed_user_ids = 42\n[planner]\nmode = magic\n")

    def test_unknown_log_level_is_refused_instead_of_silently_using_info(self):
        with self.assertRaisesRegex(ConfigError, "log_level must be one of"):
            self._load("[telegram]\nallowed_user_ids = 42\n[behaviour]\nlog_level = LOUD\n")

    def test_a_valid_log_level_is_accepted_in_any_case(self):
        config = self._load("[telegram]\nallowed_user_ids = 42\n[behaviour]\nlog_level = warning\n")
        self.assertEqual(config.log_level, "WARNING")

    def test_config_errors_never_echo_the_token(self):
        try:
            self._load(f"[telegram]\nbot_token = {TOKEN}\nallowed_user_ids = abc\n")
        except ConfigError as exc:
            self.assertNotIn(SECRET_PART, str(exc))
        else:
            self.fail("the bad user id must be refused")


# ==========================================================================================
# 7. Documentation matches the code
# ==========================================================================================

PROJECT = Path(__file__).resolve().parents[1]


class DocsTests(WireBase):

    def test_every_registered_command_is_in_the_help_text_and_the_readme(self):
        app = botmod.build_application(self.config)
        readme = (PROJECT / "README.md").read_text(encoding="utf-8")
        for name in command_names(app):
            self.assertIn(f"/{name}", botmod.MSG["help"], f"/{name} is missing from /help")
            self.assertIn(f"/{name}", readme, f"/{name} is missing from the README")

    def test_every_shipped_config_key_has_a_settings_row_in_the_readme(self):
        import configparser
        readme = (PROJECT / "README.md").read_text(encoding="utf-8")
        parser = configparser.ConfigParser()
        parser.read(PROJECT / "config.example.ini", encoding="utf-8")
        missing = []
        for section in parser.sections():
            for key in parser[section]:
                if (section, key) == ("telegram", "bot_token"):
                    continue                              # legacy, ignored; documented in the file
                if not re.search(rf"\b{re.escape(key)}\b", readme):
                    missing.append(f"[{section}] {key}")
        self.assertEqual(missing, [], "these settings are not documented in the README")

    def test_every_settings_row_in_the_readme_names_a_real_config_key(self):
        """The reverse direction: a documented key must exist in config.example.ini."""
        import configparser
        parser = configparser.ConfigParser()
        parser.read(PROJECT / "config.example.ini", encoding="utf-8")
        readme = (PROJECT / "README.md").read_text(encoding="utf-8")
        checked, unknown = 0, []
        for line in readme.splitlines():
            if not line.startswith("| `["):
                continue
            first_cell = line.split("|")[1]
            section = None
            for token in re.findall(r"`([^`]+)`", first_cell):
                m = re.fullmatch(r"\[(\w+)\] (\w+)", token)
                if m:
                    section, key = m.groups()
                elif re.fullmatch(r"\w+", token) and section:
                    key = token
                else:
                    continue
                checked += 1
                if not parser.has_option(section, key):
                    unknown.append(f"[{section}] {key}")
        self.assertGreater(checked, 25, "the settings table should name many keys")
        self.assertEqual(unknown, [], "README documents keys that config.example.ini does not have")

    def test_arabic_quick_start_explains_the_user_id_check(self):
        readme = (PROJECT / "README.md").read_text(encoding="utf-8")
        arabic = readme.split("## البدء السريع", 1)[1].split("## Quick start", 1)[0]
        self.assertIn("allowed_user_ids", arabic)
        self.assertIn("@userinfobot", arabic)
        self.assertIn("يتجاهل", arabic)

    def test_readme_explains_how_to_uninstall_and_what_is_left_behind(self):
        readme = (PROJECT / "README.md").read_text(encoding="utf-8")
        self.assertIn("## Uninstall", readme)
        for needle in (".venv", "config.ini", "token.txt", "--forget-token", "winget", "logs"):
            self.assertIn(needle, readme.split("## Uninstall", 1)[1], needle)


# ==========================================================================================
# 8. Installer (install.bat and the other batch scripts), checked as text
# ==========================================================================================

ARABIC = re.compile(r"[\u0600-\u06FF]")


class InstallerTests(IsolatedCase):

    def _labels(self) -> dict[str, str]:
        text = (PROJECT / "install.bat").read_text(encoding="utf-8")
        blocks: dict[str, str] = {}
        for match in re.finditer(r"^:(\w+)\r?\n(.*?)(?=^:\w+\r?\n|\Z)", text, re.S | re.M):
            blocks[match.group(1)] = match.group(2)
        return blocks

    def test_every_failure_message_has_an_arabic_line_as_well_as_english(self):
        blocks = self._labels()
        for label in ("no_python", "old_python", "venv_fail", "venv_broken", "pip_fail",
                      "config_fail", "end_fail"):
            self.assertIn(label, blocks)
            self.assertRegex(blocks[label], ARABIC, f":{label} has no Arabic message")
            self.assertRegex(blocks[label], r"[A-Za-z]{4,}", f":{label} has no English message")

    def test_install_bat_is_utf8_without_a_byte_order_mark(self):
        for name in ("install.bat", "start_assistant.bat"):
            raw = (PROJECT / name).read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"{name}: a BOM breaks the first cmd line")
            raw.decode("utf-8")

    def test_start_assistant_early_exits_are_bilingual(self):
        text = (PROJECT / "start_assistant.bat").read_text(encoding="utf-8")
        for english, arabic in (("The virtual environment is missing", "بالعربية: بيئة التشغيل غير موجودة"),
                                ("config.ini is missing", "بالعربية: ملف config.ini غير موجود")):
            self.assertIn(english, text)
            self.assertIn(arabic, text)

    def test_project_paths_are_quoted_so_a_folder_with_spaces_works(self):
        text = (PROJECT / "install.bat").read_text(encoding="utf-8")
        self.assertIn('cd /d "%~dp0"', text)
        self.assertNotRegex(text, r"if exist \.venv", "the venv path must be quoted in if exist")
        self.assertNotRegex(text, r"if exist config\.ini", "config.ini must be quoted in if exist")
        self.assertIn('".venv\\Scripts\\python.exe" main.py --check', text)

    def test_rerun_reuses_the_venv_and_keeps_an_existing_config(self):
        text = (PROJECT / "install.bat").read_text(encoding="utf-8")
        self.assertIn('if exist ".venv\\Scripts\\python.exe" goto venv_exists', text)
        self.assertIn('if exist "config.ini" goto config_kept', text)
        self.assertIn("left untouched", text)


if __name__ == "__main__":
    unittest.main()
