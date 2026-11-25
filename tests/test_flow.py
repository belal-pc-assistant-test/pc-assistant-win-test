"""Integration tests for the confirmation gate and plan codes.

These drive the real handlers (``on_text`` / ``on_button`` / ``_run_plan``) with
fake Telegram objects, so we can prove that:

* nothing is executed before an approver presses Yes or sends ``approve CODE``,
* pressing No / sending ``deny CODE`` executes nothing,
* a stranger, or an account that may ask but may not approve, cannot execute,
* codes are single use, exact-plan bound, and expire on re-planning,
* an approved plan really runs and reports back,
* ``/panic`` cancels everything and never runs it later.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

# Never read a real stored token during tests.
os.environ["TPA_TOKEN_STORE_DIR"] = tempfile.mkdtemp(prefix="tpa-flow-store-")
os.environ.pop("TELEGRAM_BOT_TOKEN", None)

from assistant import bot as botmod                              # noqa: E402
from assistant.bot import (                                      # noqa: E402
    PendingPlan,
    _run_plan,
    find_by_code,
    on_button,
    on_document,
    on_text,
    parse_code_command,
)
from assistant.config import load_config                         # noqa: E402
from assistant.plan_code import fingerprint                      # noqa: E402
from assistant.planner import Plan, Step                         # noqa: E402

OWNER_ID = 42
OTHER_ID = 43


def make_config(tmp: Path, ids: str = str(OWNER_ID), approvers: str | None = None, distinct: bool = False):
    cfg = tmp / "config.ini"
    body = (
        "[telegram]\n"
        "bot_token = 123456789:AAHtest_token_value_1234567890\n"
        f"allowed_user_ids = {ids}\n"
    )
    if approvers is not None:
        body += f"approver_user_ids = {approvers}\n"
    if distinct:
        body += "require_distinct_approver = true\n"
    body += (
        "\n[planner]\nmode = keywords\n"
        "\n[ollama]\nuse_ollama = false\n"
        "\n[behaviour]\ncommand_timeout = 5\n"
        "\n[paths]\n"
        f"workspace_root = {tmp.as_posix()}\n"
        f"log_file = {(tmp / 'logs' / 'a.log').as_posix()}\n"
        f"screenshots_dir = {(tmp / 'shots').as_posix()}\n"
        f"downloads_dir = {(tmp / 'downloads').as_posix()}\n"
        f"jobs_file = {(tmp / 'data' / 'jobs.json').as_posix()}\n"
    )
    cfg.write_text(body, encoding="utf-8")
    return load_config(cfg)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeBot:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.documents: list[str] = []
        self.photos = 0
        self.edits: list[str] = []

    async def send_chat_action(self, **_kwargs) -> None:
        return None

    async def send_message(self, chat_id=None, text="", **_kwargs):
        self.messages.append(text)
        return SimpleNamespace(message_id=len(self.messages))

    async def edit_message_text(self, text="", **_kwargs):
        self.edits.append(text)

    async def send_document(self, chat_id=None, document=None, filename=None, **_kwargs) -> None:
        self.documents.append(filename or "doc")

    async def send_photo(self, chat_id=None, photo=None, **_kwargs) -> None:
        self.photos += 1


class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.document = None
        self.replies: list[str] = []
        self.message_id = 1
        self.chat_id = 0

    async def reply_text(self, text, **_kwargs):
        self.replies.append(text)
        return self            # real telegram.Message has delete()/edit_text() too

    async def edit_text(self, text, **_kwargs):
        self.replies.append(text)

    async def delete(self):
        return None


class FakeQuery:
    def __init__(self, data: str, user_id: int) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id, username="tester")
        self.answered = 0
        self.edits: list[str] = []
        self.alerts: list[str] = []
        self.message = SimpleNamespace(chat_id=user_id, message_id=1)

    async def answer(self, text="", show_alert=False) -> None:
        self.answered += 1
        if show_alert:
            self.alerts.append(text)

    async def edit_message_text(self, text, **_kwargs) -> None:
        self.edits.append(text)


def make_context(config, planner=None):
    return SimpleNamespace(
        application=SimpleNamespace(
            bot_data={"config": config, "plans": {}, "planner": planner, "jobs": None}
        ),
        bot=FakeBot(),
        args=[],
    )


def make_update(query=None, message=None, user_id=OWNER_ID):
    message = message or FakeMessage()
    return SimpleNamespace(
        callback_query=query,
        effective_message=message,
        message=message,
        effective_chat=SimpleNamespace(id=user_id),
        effective_user=SimpleNamespace(id=user_id, username="tester", full_name="Tester"),
    )


def make_pending(plan: Plan, user_id: int = OWNER_ID, code: str = "K7M2QP") -> PendingPlan:
    return PendingPlan(
        plan_id="testplan0001",
        code=code,
        fingerprint=fingerprint(plan.steps),
        requester_id=user_id,
        chat_id=user_id,
        plan=plan,
        created_at=time.time(),
    )


class FakeTransport:
    """Stands in for the Telegram downloader inside receive_file."""

    def __init__(self, payload: bytes = b"hello") -> None:
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    def download_file(self, file_id: str, destination: Path) -> int:
        self.calls.append((file_id, str(destination)))
        destination.write_bytes(self.payload)
        return len(self.payload)


# ---------------------------------------------------------------------------
# Tests: buttons
# ---------------------------------------------------------------------------

class ConfirmationGateTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmpdir.name)
        self.config = make_config(self.dir)
        botmod.audit.setup(self.dir / "logs" / "a.log", "CRITICAL")  # quiet

    def tearDown(self):
        self.tmpdir.cleanup()

    def _plan(self, content: str = "written by the bot") -> tuple[Plan, Path]:
        target = self.dir / "target.txt"
        plan = Plan(
            summary="write a file",
            steps=[Step("write_file", {"path": str(target), "content": content})],
            planner="keywords",
        )
        return plan, target

    def test_deny_button_executes_nothing(self):
        plan, target = self._plan("should not exist")
        context = make_context(self.config)
        pending = make_pending(plan)
        context.application.bot_data["plans"][pending.plan_id] = pending

        query = FakeQuery(f"deny:{pending.plan_id}", OWNER_ID)
        asyncio.run(on_button(make_update(query=query), context))

        self.assertFalse(target.exists(), "deny must not execute anything")
        self.assertNotIn(pending.plan_id, context.application.bot_data["plans"])
        self.assertTrue(any("Denied" in e for e in context.bot.edits))
        self.assertEqual(query.answered, 1)

    def test_approve_button_executes(self):
        plan, target = self._plan("approved content")
        context = make_context(self.config)
        pending = make_pending(plan)
        context.application.bot_data["plans"][pending.plan_id] = pending

        query = FakeQuery(f"apv:{pending.plan_id}", OWNER_ID)
        asyncio.run(on_button(make_update(query=query), context))

        self.assertTrue(target.exists(), "approve must execute the plan")
        self.assertEqual(target.read_text(encoding="utf-8"), "approved content")
        self.assertNotIn(pending.plan_id, context.application.bot_data["plans"])
        joined = "\n".join(context.bot.messages)
        self.assertIn("Done", joined)

    def test_stranger_is_dropped_entirely(self):
        """Someone not in allowed_user_ids never reaches the handler."""
        plan, target = self._plan("hijacked")
        context = make_context(self.config)
        pending = make_pending(plan)
        context.application.bot_data["plans"][pending.plan_id] = pending

        query = FakeQuery(f"apv:{pending.plan_id}", 999)     # not authorised at all
        update = make_update(query=query, user_id=999)
        asyncio.run(on_button(update, context))

        self.assertFalse(target.exists(), "a stranger must not be able to execute")
        self.assertIn(pending.plan_id, context.application.bot_data["plans"])
        self.assertEqual(context.bot.messages, [])

    def test_non_approver_may_ask_but_not_confirm(self):
        """43 may request plans but is not an approver, so it cannot say Yes."""
        self.config = make_config(self.dir, ids=f"{OWNER_ID}, {OTHER_ID}",
                                  approvers=str(OWNER_ID))
        plan, target = self._plan("hijacked")
        context = make_context(self.config)
        pending = make_pending(plan, user_id=OTHER_ID)
        context.application.bot_data["plans"][pending.plan_id] = pending

        query = FakeQuery(f"apv:{pending.plan_id}", OTHER_ID)
        asyncio.run(on_button(make_update(query=query, user_id=OTHER_ID), context))

        self.assertFalse(target.exists(), "only an approver may confirm")
        self.assertIn(pending.plan_id, context.application.bot_data["plans"])
        self.assertTrue(any("approver" in a.lower() for a in query.alerts))

    def test_second_press_does_not_run_again(self):
        plan, target = self._plan("once")
        context = make_context(self.config)
        pending = make_pending(plan)
        context.application.bot_data["plans"][pending.plan_id] = pending

        q1 = FakeQuery(f"apv:{pending.plan_id}", OWNER_ID)
        asyncio.run(on_button(make_update(query=q1), context))
        target.unlink()

        q2 = FakeQuery(f"apv:{pending.plan_id}", OWNER_ID)
        asyncio.run(on_button(make_update(query=q2), context))
        self.assertFalse(target.exists(), "a consumed plan must not run twice")
        self.assertTrue(any("expired" in e for e in q2.edits))

    def test_unknown_plan_id_is_rejected(self):
        context = make_context(self.config)
        query = FakeQuery("apv:doesnotexist", OWNER_ID)
        asyncio.run(on_button(make_update(query=query), context))
        self.assertTrue(any("expired" in e for e in query.edits))

    def test_unauthorised_sender_is_ignored_by_text_handler(self):
        context = make_context(self.config)
        message = FakeMessage("take a screenshot")
        update = make_update(message=message, user_id=12345)
        asyncio.run(on_text(update, context))
        self.assertEqual(context.application.bot_data["plans"], {})
        self.assertEqual(message.replies, [])

    def test_failed_step_reports_error_but_continues(self):
        plan = Plan(
            summary="mixed",
            steps=[
                Step("read_file", {"path": str(self.dir / "missing.txt")}),
                Step("write_file", {"path": str(self.dir / "after.txt"), "content": "ok"}),
            ],
            planner="keywords",
        )
        context = make_context(self.config)
        pending = make_pending(plan)
        context.application.bot_data["plans"][pending.plan_id] = pending
        asyncio.run(_run_plan(make_update(), context, pending, approved_by=OWNER_ID,
                              via="button"))

        self.assertTrue((self.dir / "after.txt").exists())
        joined = "\n".join(context.bot.messages)
        self.assertIn("not found", joined.lower())
        self.assertIn("1 ok, 1 failed", joined)


# ---------------------------------------------------------------------------
# Tests: approve CODE / deny CODE
# ---------------------------------------------------------------------------

class CodeConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmpdir.name)
        self.config = make_config(self.dir)
        botmod.audit.setup(self.dir / "logs" / "a.log", "CRITICAL")
        self.context = make_context(self.config)
        self.target = self.dir / "coded.txt"
        self.plan = Plan(
            "write a file", [Step("write_file", {"path": str(self.target), "content": "hi"})],
            planner="keywords",
        )

    def tearDown(self):
        self.tmpdir.cleanup()

    def _add(self, code: str = "K7M2QP", plan: Plan | None = None) -> PendingPlan:
        pending = make_pending(plan or self.plan, code=code)
        self.context.application.bot_data["plans"][pending.plan_id] = pending
        return pending

    def _say(self, text: str, user_id: int = OWNER_ID) -> FakeMessage:
        message = FakeMessage(text)
        asyncio.run(on_text(make_update(message=message, user_id=user_id), self.context))
        return message

    # -- parsing ----------------------------------------------------------
    def test_code_command_parsing(self):
        self.assertEqual(parse_code_command("approve K7M2QP"), ("approve", "K7M2QP"))
        self.assertEqual(parse_code_command("  Deny k7m2qp "), ("deny", "K7M2QP"))
        self.assertEqual(parse_code_command("موافق K7M2QP"), ("approve", "K7M2QP"))
        self.assertIsNone(parse_code_command("approve"))
        self.assertIsNone(parse_code_command("take a screenshot"))
        self.assertIsNone(parse_code_command("please approve K7M2QP now"))

    def test_codes_are_lowercase_insensitive(self):
        self._add("K7M2QP")
        message = self._say("approve k7m2qp")
        self.assertTrue(self.target.exists(), message.replies)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "hi")

    # -- happy path / refusals -------------------------------------------
    def test_approve_by_code_runs_the_plan(self):
        self._add("K7M2QP")
        self._say("approve K7M2QP")
        self.assertTrue(self.target.exists())
        self.assertEqual(self.context.application.bot_data["plans"], {})

    def test_deny_by_code_executes_nothing(self):
        self._add("K7M2QP")
        self._say("deny K7M2QP")
        self.assertFalse(self.target.exists())
        self.assertEqual(self.context.application.bot_data["plans"], {})

    def test_wrong_code_is_rejected(self):
        self._add("K7M2QP")
        message = self._say("approve ZZZZZZ")
        self.assertFalse(self.target.exists())
        self.assertTrue(any("No live plan" in r for r in message.replies))
        self.assertIn("K7M2QP", "\n".join(message.replies))   # hints the live code
        self.assertIsNotNone(find_by_code(self.context.application.bot_data["plans"], "K7M2QP"))

    def test_code_is_single_use(self):
        self._add("K7M2QP")
        self._say("approve K7M2QP")
        self.target.unlink()
        message = self._say("approve K7M2QP")           # replay
        self.assertFalse(self.target.exists())
        self.assertTrue(any("No live plan" in r for r in message.replies))

    def test_code_only_matches_its_own_plan(self):
        """Two plans: the code of one must never approve the other."""
        other_target = self.dir / "other.txt"
        other = Plan("other", [Step("write_file",
                                    {"path": str(other_target), "content": "other"})],
                     planner="keywords")
        self._add("AAAAAA", plan=self.plan)
        self._add("BBBBBB", plan=other)

        self._say("approve BBBBBB")
        self.assertTrue(other_target.exists())
        self.assertFalse(self.target.exists(), "the other plan must not run")

    def test_replanning_voids_the_old_code(self):
        """The same request again creates a new code and burns the old one."""
        planner = SimpleNamespace(plan=lambda _req: self.plan)
        self.context.application.bot_data["planner"] = planner

        first = self._say("write the file")
        first_code = next(iter(self.context.application.bot_data["plans"].values())).code
        second = self._say("write the file")
        self.assertTrue(first.replies or second.replies)
        store = self.context.application.bot_data["plans"]
        self.assertEqual(len(store), 1, "only one plan may wait per chat")

        new_code = next(iter(store.values())).code
        self.assertNotEqual(first_code, new_code)
        self.assertIsNone(find_by_code(store, first_code))

        message = self._say(f"approve {first_code}")
        self.assertFalse(self.target.exists())
        self.assertTrue(any("No live plan" in r for r in message.replies))

    def test_non_approver_cannot_use_a_code(self):
        self.config = make_config(self.dir, ids=f"{OWNER_ID}, {OTHER_ID}",
                                  approvers=str(OWNER_ID))
        self.context.application.bot_data["config"] = self.config
        self._add("K7M2QP")

        message = self._say("approve K7M2QP", user_id=OTHER_ID)
        self.assertFalse(self.target.exists())
        self.assertTrue(any("may request plans but not approve" in r for r in message.replies))
        self.assertIsNotNone(find_by_code(self.context.application.bot_data["plans"], "K7M2QP"))

    def test_unknown_user_cannot_use_a_code(self):
        self._add("K7M2QP")
        self._say("approve K7M2QP", user_id=999)
        self.assertFalse(self.target.exists())

    def test_fingerprint_is_shown_and_matches_the_steps(self):
        plan = self.plan
        text = botmod.render_plan_message(plan, "K7M2QP", fingerprint(plan.steps))
        self.assertIn("K7M2QP", text)
        self.assertIn(fingerprint(plan.steps), text)
        # Changing a step changes the fingerprint.
        plan.steps[0].params["content"] = "something else"
        self.assertNotIn(fingerprint(plan.steps), text)

    def test_tampered_plan_is_refused(self):
        pending = self._add("K7M2QP")
        pending.plan.steps[0].params["content"] = "tampered"      # invalidates fingerprint
        message = self._say("approve K7M2QP")
        self.assertFalse(self.target.exists())
        self.assertTrue(any("fingerprint" in r.lower() for r in message.replies))


# ---------------------------------------------------------------------------
# Tests: /panic, uploads
# ---------------------------------------------------------------------------

class PanicAndUploadTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmpdir.name)
        self.config = make_config(self.dir)
        botmod.audit.setup(self.dir / "logs" / "a.log", "CRITICAL")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_panic_cancels_every_pending_plan(self):
        target_a = self.dir / "a.txt"
        target_b = self.dir / "b.txt"
        context = make_context(self.config)
        for name, target in (("A", target_a), ("B", target_b)):
            plan = Plan(name, [Step("write_file", {"path": str(target), "content": name})],
                        planner="keywords")
            pending = make_pending(plan, code=f"AAAA{name}")
            context.application.bot_data["plans"][pending.plan_id] = pending

        update = make_update()
        asyncio.run(botmod.cmd_panic(update, context))

        self.assertEqual(context.application.bot_data["plans"], {})
        self.assertFalse(target_a.exists())
        self.assertFalse(target_b.exists())
        self.assertTrue(any("Panic" in r for r in update.message.replies))

        # And the cancelled codes stay dead.
        message = FakeMessage("approve AAAAA")
        asyncio.run(on_text(make_update(message=message), context))
        self.assertFalse(target_a.exists())
        self.assertTrue(any("No live plan" in r for r in message.replies))

    def test_panic_clears_the_abort_flag_for_the_next_plan(self):
        from assistant.actions.base import abort_requested, clear_abort

        context = make_context(self.config)
        asyncio.run(botmod.cmd_panic(make_update(), context))
        self.assertTrue(abort_requested())
        clear_abort()
        self.assertFalse(abort_requested())

    def test_upload_creates_a_confirmed_download_plan(self):
        context = make_context(self.config)
        message = FakeMessage()
        message.document = SimpleNamespace(file_id="FID123", file_name="report.txt",
                                           file_size=5)
        asyncio.run(on_document(make_update(message=message), context))

        store = context.application.bot_data["plans"]
        self.assertEqual(len(store), 1)
        pending = next(iter(store.values()))
        self.assertEqual(pending.plan.steps[0].action, "receive_file")
        self.assertIn("report.txt", pending.plan.steps[0].params["target"])
        # The plan text with the code was sent to the requester.
        self.assertTrue(any(pending.code in m for m in context.bot.messages))

        # Nothing was downloaded yet.
        target = Path(pending.plan.steps[0].params["target"])
        self.assertFalse(target.exists(), "an upload must still be approved")

        transport = FakeTransport(b"hello")
        original = botmod.TelegramTransport
        botmod.TelegramTransport = lambda *_a, **_k: transport      # noqa: E731
        try:
            reply = FakeMessage(f"approve {pending.code}")
            asyncio.run(on_text(make_update(message=reply), context))
        finally:
            botmod.TelegramTransport = original

        self.assertTrue(target.exists(), "approving the upload must save the file")
        self.assertEqual(target.read_bytes(), b"hello")
        self.assertEqual(transport.calls[0][0], "FID123")

    def test_oversized_upload_is_refused_before_confirmation(self):
        context = make_context(self.config)
        message = FakeMessage()
        message.document = SimpleNamespace(file_id="FID", file_name="huge.bin",
                                           file_size=500 * 1024 * 1024)
        asyncio.run(on_document(make_update(message=message), context))
        self.assertEqual(context.application.bot_data["plans"], {})
        self.assertTrue(any("limit" in r.lower() for r in message.replies))


if __name__ == "__main__":
    unittest.main(verbosity=2)
