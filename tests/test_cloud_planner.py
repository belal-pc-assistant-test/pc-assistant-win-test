"""Cloud (Pollinations) planner tests against a local mock server.

Covers: happy path, request body contract, HTTP 429 rate limiting + cooldown,
5xx errors, timeouts, dead endpoints, non-JSON and malformed payloads, the
response_format retry, and the cloud → local → keywords fallback chain.
"""

from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "tests"))

from _mock_http import QuietHandler, start_server, stop_server   # noqa: E402
from assistant.planner import (                                    # noqa: E402
    CloudPlanner,
    LocalPlanner,
    Planner,
    PlannerError,
    PlanValidationError,
    _is_reset_or_abort,
)

GOOD_PLAN = {
    "summary": "Take a screenshot and send it",
    "steps": [{"action": "screenshot", "params": {},
               "description": "capture the screen"}],
}


class MockPollinations(QuietHandler):
    behavior = "ok"
    requests_seen: list[dict] = []
    resets_left = 0          # for "reset_once": how many requests to reset before answering

    def do_POST(self) -> None:                                  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        MockPollinations.requests_seen.append(body)

        mode = MockPollinations.behavior

        if mode == "reset_always":
            return self.reset_connection()
        if mode == "reset_once" and MockPollinations.resets_left > 0:
            MockPollinations.resets_left -= 1
            return self.reset_connection()
        if mode == "reject_format" and "response_format" in body:
            return self._json({"error": {"message": "unknown field response_format"}}, 400)
        if mode == "429":
            return self._json({"error": "rate limited"}, 429, {"Retry-After": "30"})
        if mode == "402":
            return self._json({
                "status": 402,
                "success": False,
                "error": {
                    "code": "FREE_LIMIT",
                    "message": "Free limit reached: 1 reply every 45 seconds per IP. "
                               "Retry after 21s, or get unlimited access.",
                },
            }, 402)
        if mode == "500":
            return self._json({"error": "boom"}, 500)
        if mode == "slow":
            time.sleep(0.5)
            return self._json(self._completion(json.dumps(GOOD_PLAN)))
        if mode == "badjson":
            return self._json(self._completion("I am sorry, I cannot do that."))
        if mode == "empty":
            return self._json({"choices": [{"message": {"content": ""}}]})
        if mode == "nochoices":
            return self._json({"unexpected": True})
        if mode == "notjson":
            payload = b"<html>gateway error</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.write_safely(payload)
            return
        return self._json(self._completion(json.dumps(GOOD_PLAN, ensure_ascii=False)))

    @staticmethod
    def _completion(content: str) -> dict:
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}

    def _json(self, obj: dict, status: int = 200, headers: dict | None = None) -> None:
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.write_safely(payload)  # a client that timed out on purpose is not an error


class CloudPlannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = start_server(MockPollinations)
        cls.port = cls.server.server_address[1]
        cls.url = f"http://127.0.0.1:{cls.port}/openai"

    @classmethod
    def tearDownClass(cls) -> None:
        stop_server(cls.server)

    def setUp(self) -> None:
        MockPollinations.behavior = "ok"
        MockPollinations.requests_seen = []
        MockPollinations.resets_left = 0
        self.tmp = Path(PROJECT_DIR)  # any existing folder works as workspace

    def planner(self, url: str | None = None, timeout: float = 5.0) -> CloudPlanner:
        return CloudPlanner(url=url or self.url, model="openai", timeout=timeout)

    # -- happy path -----------------------------------------------------
    def test_successful_plan(self):
        plan = self.planner().plan("take a screenshot", self.tmp, 6)
        self.assertEqual(plan.planner, "cloud")
        self.assertEqual(plan.model, "openai")
        self.assertEqual(plan.steps[0].action, "screenshot")
        self.assertIn("pollinations", plan.label())

    def test_request_contract(self):
        self.planner().plan("start chrome", self.tmp, 6)
        body = MockPollinations.requests_seen[-1]
        self.assertEqual(body["model"], "openai")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertIn("messages", body)
        messages = body["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("NEVER invent", messages[0]["content"])
        self.assertIn("Arabic", messages[0]["content"])
        self.assertEqual(messages[-1]["role"], "user")
        self.assertIn("start chrome", messages[-1]["content"])
        # few-shot turns are included
        assistant_turns = [m for m in messages if m["role"] == "assistant"]
        self.assertGreaterEqual(len(assistant_turns), 6)
        for turn in assistant_turns:
            json.loads(turn["content"])  # every example is valid JSON

    def test_arabic_plan_roundtrip(self):
        MockPollinations.behavior = "ok"
        raw = {"summary": "أخذ لقطة شاشة",
               "steps": [{"action": "screenshot", "params": {},
                          "description": "تصوير الشاشة"}]}
        original = MockPollinations.do_POST

        def patched(handler):
            payload = json.dumps(
                {"choices": [{"message": {"content": json.dumps(raw, ensure_ascii=False)}}]}
            ).encode()
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.write_safely(payload)

        MockPollinations.do_POST = patched
        try:
            plan = self.planner().plan("خد لقطة شاشة", self.tmp, 6)
        finally:
            MockPollinations.do_POST = original
        self.assertEqual(plan.summary, "أخذ لقطة شاشة")
        self.assertEqual(plan.steps[0].description, "تصوير الشاشة")

    # -- failures -------------------------------------------------------
    def test_rate_limit_sets_cooldown(self):
        MockPollinations.behavior = "429"
        cloud = self.planner()
        with self.assertRaises(PlannerError) as ctx:
            cloud.plan("take a screenshot", self.tmp, 6)
        self.assertIn("429", str(ctx.exception))
        cooling, why = cloud.cooling_down()
        self.assertTrue(cooling)
        self.assertIn("retry", why)

        calls_before = len(MockPollinations.requests_seen)
        with self.assertRaises(PlannerError) as ctx2:
            cloud.plan("take a screenshot", self.tmp, 6)
        self.assertIn("paused", str(ctx2.exception))
        self.assertEqual(len(MockPollinations.requests_seen), calls_before,
                         "no request should be sent while cooling down")

    def test_free_tier_402_uses_the_server_retry_hint(self):
        MockPollinations.behavior = "402"
        cloud = self.planner()
        with self.assertRaises(PlannerError) as ctx:
            cloud.plan("x", self.tmp, 6)
        self.assertIn("free-tier limit", str(ctx.exception))
        cooling, why = cloud.cooling_down()
        self.assertTrue(cooling)
        # the body says "Retry after 21s" — we pause ~21s, not the 120s default
        minutes = int(why.split("in ")[1].replace("s", ""))
        self.assertLessEqual(minutes, 25)

    def test_server_error(self):
        MockPollinations.behavior = "500"
        with self.assertRaises(PlannerError) as ctx:
            self.planner().plan("x", self.tmp, 6)
        self.assertIn("500", str(ctx.exception))

    def test_timeout(self):
        MockPollinations.behavior = "slow"
        with self.assertRaises(PlannerError) as ctx:
            self.planner(timeout=0.1).plan("x", self.tmp, 6)
        self.assertIn("timed out", str(ctx.exception))

    def test_unreachable_endpoint(self):
        with self.assertRaises(PlannerError) as ctx:
            self.planner(url="http://127.0.0.1:9/openai", timeout=2).plan("x", self.tmp, 6)
        message = str(ctx.exception)
        # Linux reports a refused connect as "unreachable"; a slow Windows
        # connect surfaces as "timed out". Either way the planner must fail
        # safely (PlannerError -> the facade falls back to the next backend).
        self.assertTrue(
            "unreachable" in message or "timed out" in message, message
        )

    def test_non_json_reply(self):
        MockPollinations.behavior = "notjson"
        with self.assertRaises(PlannerError) as ctx:
            self.planner().plan("x", self.tmp, 6)
        self.assertIn("non-JSON", str(ctx.exception))

    def test_model_talked_instead_of_json(self):
        MockPollinations.behavior = "badjson"
        with self.assertRaises(PlanValidationError):
            self.planner().plan("x", self.tmp, 6)

    def test_empty_answer(self):
        MockPollinations.behavior = "empty"
        with self.assertRaises(PlannerError):
            self.planner().plan("x", self.tmp, 6)

    def test_missing_choices(self):
        MockPollinations.behavior = "nochoices"
        with self.assertRaises(PlannerError) as ctx:
            self.planner().plan("x", self.tmp, 6)
        self.assertIn("choices", str(ctx.exception))

    def test_response_format_rejected_is_retried_without_it(self):
        MockPollinations.behavior = "reject_format"
        plan = self.planner().plan("take a screenshot", self.tmp, 6)
        self.assertEqual(plan.steps[0].action, "screenshot")
        self.assertEqual(len(MockPollinations.requests_seen), 2)
        self.assertIn("response_format", MockPollinations.requests_seen[0])
        self.assertNotIn("response_format", MockPollinations.requests_seen[1])

    def test_response_format_can_be_disabled(self):
        cloud = CloudPlanner(url=self.url, model="openai", timeout=5,
                             use_response_format=False)
        cloud.plan("x", self.tmp, 6)
        self.assertNotIn("response_format", MockPollinations.requests_seen[-1])


class FallbackChainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = start_server(MockPollinations)
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/openai"

    @classmethod
    def tearDownClass(cls) -> None:
        stop_server(cls.server)

    def setUp(self) -> None:
        MockPollinations.behavior = "ok"
        MockPollinations.requests_seen = []
        MockPollinations.resets_left = 0

    def dead_local(self) -> LocalPlanner:
        return LocalPlanner("http://127.0.0.1:9", "qwen2.5:3b", timeout=1)

    def test_cloud_is_used_first(self):
        planner = Planner(mode="cloud", cloud=CloudPlanner(url=self.url, timeout=5),
                          local=self.dead_local(), workspace_root=PROJECT_DIR)
        plan = planner.plan("take a screenshot")
        self.assertEqual(plan.planner, "cloud")
        self.assertEqual(len(MockPollinations.requests_seen), 1)

    def test_cloud_down_falls_back_to_keywords(self):
        planner = Planner(mode="cloud",
                          cloud=CloudPlanner(url="http://127.0.0.1:9/openai", timeout=1),
                          local=self.dead_local(), workspace_root=PROJECT_DIR)
        plan = planner.plan("take a screenshot")
        self.assertEqual(plan.planner, "keywords")
        self.assertEqual(plan.steps[0].action, "screenshot")
        self.assertIn("cloud:", plan.note)
        self.assertIn("local:", plan.note)

    def test_keywords_mode_never_touches_the_network(self):
        planner = Planner(mode="keywords",
                          cloud=CloudPlanner(url=self.url, timeout=5),
                          local=self.dead_local(), workspace_root=PROJECT_DIR)
        plan = planner.plan("take a screenshot")
        self.assertEqual(plan.planner, "keywords")
        self.assertEqual(MockPollinations.requests_seen, [])

    def test_cooldown_skips_cloud_on_next_request(self):
        cloud = CloudPlanner(url=self.url, timeout=5, cooldown_seconds=30)
        MockPollinations.behavior = "429"
        planner = Planner(mode="cloud", cloud=cloud, local=self.dead_local(),
                          workspace_root=PROJECT_DIR)
        first = planner.plan("take a screenshot")
        self.assertEqual(first.planner, "keywords")
        calls = len(MockPollinations.requests_seen)

        MockPollinations.behavior = "ok"
        second = planner.plan("take a screenshot")
        self.assertEqual(len(MockPollinations.requests_seen), calls,
                         "cloud must stay paused during the cooldown")
        self.assertEqual(second.planner, "keywords")

    def test_step_cap_applies_to_model_output(self):
        six = {"summary": "s", "steps": [{"action": "screenshot", "params": {}}
                                         for _ in range(6)]}
        original = MockPollinations.do_POST

        def patched(handler):
            payload = json.dumps({"choices": [{"message": {"content": json.dumps(six)}}]}).encode()
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.write_safely(payload)

        MockPollinations.do_POST = patched
        try:
            planner = Planner(mode="cloud", cloud=CloudPlanner(url=self.url, timeout=5),
                              workspace_root=PROJECT_DIR, max_steps=2)
            plan = planner.plan("take a screenshot")
        finally:
            MockPollinations.do_POST = original
        self.assertEqual(len(plan.steps), 2)

    def test_status_reports_backends(self):
        planner = Planner(mode="cloud", cloud=CloudPlanner(url=self.url, timeout=5),
                          local=self.dead_local(), workspace_root=PROJECT_DIR)
        info = planner.status()
        self.assertEqual(info["mode"], "cloud")
        self.assertEqual(info["chain"], ["cloud", "local", "keywords"])
        self.assertIn("url", info["cloud"])
        self.assertFalse(info["local"]["reachable"])


class ResetRetryTests(unittest.TestCase):
    """A connection reset or abort is retried once, after a pause. Nothing else is."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = start_server(MockPollinations)
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/openai"

    @classmethod
    def tearDownClass(cls) -> None:
        stop_server(cls.server)

    def setUp(self) -> None:
        MockPollinations.behavior = "ok"
        MockPollinations.requests_seen = []
        MockPollinations.resets_left = 0
        self.tmp = Path(PROJECT_DIR)

    def planner_fast(self) -> CloudPlanner:
        return CloudPlanner(url=self.url, model="openai", timeout=5, reset_retry_seconds=0)

    def test_one_reset_then_success_returns_the_plan(self):
        MockPollinations.behavior = "reset_once"
        MockPollinations.resets_left = 1
        plan = self.planner_fast().plan("take a screenshot", self.tmp, 6)
        self.assertEqual(plan.planner, "cloud")
        self.assertEqual(plan.steps[0].action, "screenshot")
        self.assertEqual(len(MockPollinations.requests_seen), 2, "one failed attempt, one retry")

    def test_the_retry_does_not_start_a_cooldown(self):
        MockPollinations.behavior = "reset_once"
        MockPollinations.resets_left = 1
        cloud = self.planner_fast()
        cloud.plan("take a screenshot", self.tmp, 6)
        self.assertFalse(cloud.cooling_down()[0])

    def test_two_resets_in_a_row_fall_back_with_the_usual_message(self):
        MockPollinations.behavior = "reset_always"
        with self.assertRaises(PlannerError) as ctx:
            self.planner_fast().plan("x", self.tmp, 6)
        self.assertIn("unreachable", str(ctx.exception))
        self.assertEqual(len(MockPollinations.requests_seen), 2, "exactly one retry, never a loop")

    def test_the_retry_waits_the_default_pause(self):
        MockPollinations.behavior = "reset_once"
        MockPollinations.resets_left = 1
        cloud = CloudPlanner(url=self.url, model="openai", timeout=5)  # default pause
        with mock.patch("assistant.planner.time.sleep") as sleep:
            cloud.plan("x", self.tmp, 6)
        sleep.assert_called_once_with(CloudPlanner.RESET_RETRY_SECONDS)

    def test_the_server_keeps_serving_after_it_reset_a_connection(self):
        MockPollinations.behavior = "reset_always"
        with self.assertRaises(PlannerError):
            self.planner_fast().plan("x", self.tmp, 6)
        MockPollinations.behavior = "ok"
        plan = self.planner_fast().plan("take a screenshot", self.tmp, 6)
        self.assertEqual(plan.planner, "cloud")

    def test_a_refused_connection_is_not_retried(self):
        cloud = CloudPlanner(url="http://127.0.0.1:9/openai", model="openai", timeout=2,
                             reset_retry_seconds=0)
        with mock.patch("assistant.planner.time.sleep") as sleep:
            with self.assertRaises(PlannerError) as ctx:
                cloud.plan("x", self.tmp, 6)
        self.assertIn("unreachable", str(ctx.exception))
        sleep.assert_not_called()

    def test_a_timeout_is_not_retried(self):
        MockPollinations.behavior = "slow"
        cloud = CloudPlanner(url=self.url, model="openai", timeout=0.1, reset_retry_seconds=0)
        with self.assertRaises(PlannerError) as ctx:
            cloud.plan("x", self.tmp, 6)
        self.assertIn("timed out", str(ctx.exception))
        self.assertEqual(len(MockPollinations.requests_seen), 1)


class ResetClassifierTests(unittest.TestCase):
    """Which failures count as a reset or abort. No network involved."""

    def _wrapped(self, inner: BaseException):
        import requests
        from urllib3.exceptions import ProtocolError
        return requests.exceptions.ConnectionError(ProtocolError("Connection aborted.", inner))

    def test_reset_and_abort_are_retryable(self):
        self.assertTrue(_is_reset_or_abort(ConnectionResetError(10054, "reset")))
        self.assertTrue(_is_reset_or_abort(ConnectionAbortedError(10053, "aborted")))
        self.assertTrue(_is_reset_or_abort(BrokenPipeError()))

    def test_requests_wrapped_resets_are_retryable(self):
        self.assertTrue(_is_reset_or_abort(self._wrapped(ConnectionResetError("peer reset"))))
        self.assertTrue(_is_reset_or_abort(self._wrapped(ConnectionAbortedError("aborted"))))

    def test_remote_disconnect_is_retryable(self):
        from http.client import RemoteDisconnected
        self.assertTrue(_is_reset_or_abort(self._wrapped(RemoteDisconnected("closed"))))

    def test_refused_timeout_and_other_errors_are_not_retryable(self):
        self.assertFalse(_is_reset_or_abort(ConnectionRefusedError(10061, "refused")))
        self.assertFalse(_is_reset_or_abort(self._wrapped(ConnectionRefusedError("refused"))))
        self.assertFalse(_is_reset_or_abort(OSError("no route")))
        self.assertFalse(_is_reset_or_abort(TimeoutError("slow")))

    def test_the_walk_terminates_on_a_cycle(self):
        a = RuntimeError("a")
        b = RuntimeError("b")
        a.__context__ = b
        b.__context__ = a
        self.assertFalse(_is_reset_or_abort(a))


if __name__ == "__main__":
    unittest.main(verbosity=2)
