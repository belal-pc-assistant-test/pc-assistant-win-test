"""/remote through the real bot handlers: owner gate, plan approval, stop/panic, planner exclusion, config, audit."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from _bootstrap import PROJECT_DIR
from test_flow import OTHER_ID, OWNER_ID, FakeMessage, make_config, make_context, make_update
from test_remote_session import FakeOsTestCase, Harness, FakeTunnel

from assistant import audit
from assistant import bot as botmod
from assistant.action_spec import VALID_ACTIONS
from assistant.actions import clear_abort, execute_step
from assistant.config import ConfigError, load_config
from assistant.planner import PlanValidationError, Step, _validate_builtin

assert PROJECT_DIR.is_dir()
MARKER = "remote-test-marker"


class RemoteBotTests(FakeOsTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmpdir.name)
        self.config = make_config(self.dir)
        audit.setup(self.dir / "logs" / "a.log", "CRITICAL")
        self.context = make_context(self.config)
        self.harness = Harness(self)
        self.factory_calls = 0
        data = self.context.application.bot_data

        def factory(chat_id):
            self.factory_calls += 1
            return self.harness.deps

        data["remote_deps_factory"] = factory
        data["remote_platform_ok"] = lambda: True

    def tearDown(self) -> None:
        clear_abort()
        audit.close()
        self.tmpdir.cleanup()
        super().tearDown()

    async def run_remote(self, args: list[str], user_id: int = OWNER_ID) -> FakeMessage:
        message = FakeMessage("/remote " + " ".join(args))
        self.context.args = args
        await botmod.cmd_remote(make_update(message=message, user_id=user_id), self.context)
        return message

    async def say_text(self, text: str, user_id: int = OWNER_ID) -> FakeMessage:
        message = FakeMessage(text)
        await botmod.on_text(make_update(message=message, user_id=user_id), self.context)
        return message

    def pending_remote(self):
        return [p for p in self.context.application.bot_data["plans"].values()
                if any(s.action == "remote_start" for s in p.plan.steps)]

    def code_of(self) -> str:
        return self.pending_remote()[0].code


class RemoteBotGateTests(RemoteBotTests):
    async def test_only_the_allowed_user_can_request_a_session(self):
        await self.run_remote(["start", "5"], user_id=OTHER_ID)
        self.assertEqual(self.pending_remote(), [])
        self.assertEqual(self.factory_calls, 0)
        self.assertIsNone(self.context.application.bot_data.get("remote_controller"))

    async def test_start_only_proposes_a_plan_and_starts_nothing(self):
        await self.run_remote(["start", "5"])
        pending = self.pending_remote()
        self.assertEqual(len(pending), 1)
        summary = pending[0].plan.summary
        self.assertIn("FULL VIEW AND CONTROL for 5 minute(s)", summary)
        self.assertIn("mouse and keyboard", summary)
        self.assertEqual(self.factory_calls, 0, "no capture, tunnel or banner before approval")
        self.assertEqual(self.harness.messages, [])

    async def test_default_is_15_minutes_and_above_60_is_refused(self):
        await self.run_remote(["start"])
        self.assertIn("for 15 minute(s)", self.pending_remote()[0].plan.summary)
        before = len(self.pending_remote())
        reply = await self.run_remote(["start", "61"])
        self.assertEqual(len(self.pending_remote()), before)
        self.assertIn("1–60", reply.replies[-1])

    async def test_zero_negative_and_garbage_durations_are_refused(self):
        for raw in ("0", "-5", "abc", "1.5"):
            with self.subTest(raw=raw):
                reply = await self.run_remote(["start", raw])
                self.assertTrue(reply.replies)
        self.assertEqual(self.pending_remote(), [])

    async def test_group_chat_cannot_start(self):
        message = FakeMessage("/remote start 5")
        self.context.args = ["start", "5"]
        update = make_update(message=message, user_id=OWNER_ID)
        update.effective_chat = SimpleNamespace(id=-100, type="group")
        await botmod.cmd_remote(update, self.context)
        self.assertEqual(self.pending_remote(), [])

    async def test_usage_is_shown_for_unknown_subcommands(self):
        reply = await self.run_remote(["explode"])
        self.assertIn("Usage: /remote start", reply.replies[-1])
        self.assertEqual(self.pending_remote(), [])

    async def test_remote_session_cannot_be_saved_as_a_macro(self):
        await self.run_remote(["start", "3"])
        message = FakeMessage("/macro save sneaky")
        self.context.args = ["save", "sneaky"]
        await botmod.cmd_macro(make_update(message=message), self.context)
        self.assertIn("cannot be saved as macros", message.replies[-1])
        self.assertEqual(self.pending_remote()[0].plan.steps[0].action, "remote_start")
        memory_plans = [p for p in self.context.application.bot_data["plans"].values()
                        if any(st.action == "memory_update" for st in p.plan.steps)]
        self.assertEqual(memory_plans, [], "no save proposal may be created for a remote plan")

    async def test_help_text_lists_remote_commands(self):
        message = FakeMessage("/help")
        await botmod.cmd_help(make_update(message=message), self.context)
        text = "\n".join(message.replies)
        self.assertIn("/remote start", text)
        self.assertIn("/remote stop", text)


class RemoteBotApprovalTests(RemoteBotTests):
    async def test_approve_by_code_starts_the_session_and_sends_one_link(self):
        await self.run_remote(["start", "3"])
        code = self.code_of()
        await self.say_text(f"approve {code}")
        controller = self.context.application.bot_data["remote_controller"]
        self.assertIsNotNone(controller.active)
        self.assertEqual(controller.active.state, "active")
        links = [m for m in self.harness.messages if "/connect/" in m]
        self.assertEqual(len(links), 1)
        self.assertIn("FULL VIEW and CONTROL", links[0])
        self.assertEqual(self.pending_remote(), [], "the plan is consumed once approved")
        await controller.stop("requested")

    async def test_deny_starts_nothing(self):
        await self.run_remote(["start", "3"])
        await self.say_text(f"deny {self.code_of()}")
        self.assertIsNone(self.context.application.bot_data.get("remote_controller").active)
        self.assertEqual(self.harness.messages, [])
        self.assertEqual(self.pending_remote(), [])

    async def test_a_second_approval_cannot_restart_a_consumed_plan(self):
        await self.run_remote(["start", "3"])
        code = self.code_of()
        await self.say_text(f"approve {code}")
        await self.say_text(f"approve {code}")
        links = [m for m in self.harness.messages if "/connect/" in m]
        self.assertEqual(len(links), 1)
        await self.context.application.bot_data["remote_controller"].stop("requested")

    async def test_status_never_shows_the_link(self):
        await self.run_remote(["start", "3"])
        await self.say_text(f"approve {self.code_of()}")
        reply = await self.run_remote(["status"])
        text = reply.replies[-1]
        self.assertIn("Remote session", text)
        link = [m for m in self.harness.messages if "/connect/" in m][0]
        secret = link.split("/connect/")[1].split()[0]
        self.assertNotIn(secret, text)
        self.assertNotIn("/connect/", text)
        await self.context.application.bot_data["remote_controller"].stop("requested")

    async def test_stop_cancels_a_pending_request_so_it_cannot_be_approved_later(self):
        await self.run_remote(["start", "3"])
        code = self.code_of()
        await self.run_remote(["stop"])
        self.assertEqual(self.pending_remote(), [])
        await self.say_text(f"approve {code}")
        self.assertEqual(self.factory_calls, 0)

    async def test_stale_plan_after_a_stop_epoch_is_refused_at_approval(self):
        await self.run_remote(["start", "3"])
        code = self.code_of()
        # Simulate a stop racing the approval: the epoch moves but the pending plan survives.
        self.context.application.bot_data["remote_epoch"] = 99
        await self.say_text(f"approve {code}")
        self.assertEqual(self.factory_calls, 0)
        self.assertIsNone(self.context.application.bot_data["remote_controller"].active)

    async def test_stop_while_running_ends_the_session_and_closes_the_tunnel(self):
        await self.run_remote(["start", "3"])
        await self.say_text(f"approve {self.code_of()}")
        session = self.context.application.bot_data["remote_controller"].active
        tunnel = FakeTunnel.instances[-1]
        reply = await self.run_remote(["stop"])
        self.assertEqual(session.state, "stopped")
        self.assertTrue(tunnel.closed)
        self.assertIn("Remote session stopped", reply.replies[-1])

    async def test_panic_stops_a_running_session_and_its_tunnel(self):
        await self.run_remote(["start", "3"])
        await self.say_text(f"approve {self.code_of()}")
        session = self.context.application.bot_data["remote_controller"].active
        tunnel = FakeTunnel.instances[-1]
        reply = FakeMessage("/panic")
        await botmod.cmd_panic(make_update(message=reply), self.context)
        self.assertEqual(session.state, "stopped")
        self.assertTrue(tunnel.closed)
        self.assertTrue(any("Remote session stopped: yes" in m for m in reply.replies + self.harness.messages
                            + [self.context.bot.messages[-1]]))


class RemoteBotBoundaryTests(RemoteBotTests):
    async def test_remote_start_is_not_a_planner_action(self):
        self.assertNotIn("remote_start", VALID_ACTIONS)

    async def test_generic_executor_refuses_remote_start(self):
        result = execute_step(Step("remote_start", {"minutes": "5"}), self.config)
        self.assertFalse(result.ok)
        self.assertEqual(self.factory_calls, 0)

    async def test_planner_validator_rejects_remote_start_from_a_model(self):
        model_plan = {"summary": "start remote", "steps": [
            {"action": "remote_start", "params": {"minutes": "5"}}]}
        with self.assertRaises(PlanValidationError):
            _validate_builtin(model_plan, 5)
        # A well-formed plan for a known action still passes, so the check is not vacuous.
        _validate_builtin({"summary": "list files", "steps": [
            {"action": "jobs_read", "params": {}}]}, 5)


class RemoteConfigTests(unittest.TestCase):
    def write(self, body: str) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="tpa-cfg-"))
        make_config(tmp)  # writes a valid base config; our section is appended below
        path = tmp / "config.ini"
        path.write_text(path.read_text(encoding="utf-8") + "\n" + body, encoding="utf-8")
        return path

    def test_defaults_are_15_and_60(self):
        config = make_config(Path(tempfile.mkdtemp(prefix="tpa-cfg-")))
        self.assertEqual((config.remote_default_minutes, config.remote_max_minutes), (15, 60))
        self.assertEqual(config.remote_tunnel_order, ["cloudflare", "localhost.run"])
        self.assertEqual(config.cloudflared_sha256, "")

    def test_limits_outside_the_allowed_range_are_rejected(self):
        for body in ("[remote]\nmax_minutes = 61\n", "[remote]\nmax_minutes = 0\n",
                     "[remote]\ndefault_minutes = 90\n"):
            with self.subTest(body=body):
                with self.assertRaises(ConfigError):
                    load_config(self.write(body))

    def test_unknown_provider_and_bad_pin_are_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(self.write("[remote]\ntunnel_order = ngrok\n"))
        with self.assertRaises(ConfigError):
            load_config(self.write("[remote]\ncloudflared_sha256 = not-a-hash\n"))

    def test_valid_pin_is_normalised(self):
        config = load_config(self.write("[remote]\ncloudflared_sha256 = " + "AB" * 32 + "\n"))
        self.assertEqual(config.cloudflared_sha256, "ab" * 32)


class RemoteAuditTests(unittest.TestCase):
    def test_remote_events_keep_numbers_and_safe_labels_only(self):
        with self.assertLogs("assistant", level="INFO") as logs:
            audit.event("remote_stopped", user_id=1, reason="banner lost", frames=12, inputs=4,
                        viewers=1, rejected=0, provider="cloudflare",
                        link="https://x.trycloudflare.com/connect/SECRET", text="typed secret")
        line = logs.output[-1]
        self.assertIn('"reason": "banner lost"', line)
        self.assertIn('"frames": 12', line)
        self.assertNotIn("SECRET", line)
        self.assertNotIn("trycloudflare", line)
        self.assertNotIn("typed secret", line)

    def test_text_labels_that_look_like_tokens_are_dropped(self):
        with self.assertLogs("assistant", level="INFO") as logs:
            audit.event("remote_stopped", reason="https://a.trycloudflare.com", provider="Ab3dEf9GhIj")
        line = logs.output[-1]
        self.assertNotIn("trycloudflare", line)
        self.assertNotIn("Ab3dEf9", line)


if __name__ == "__main__":
    unittest.main()
