"""Local (Ollama) planner tests against a mock Ollama server.

No real Ollama installation is needed: a tiny HTTP server pretends to be
Ollama on a random localhost port.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "tests"))

from _mock_http import QuietHandler, start_server, stop_server  # noqa: E402

from assistant.planner import LocalPlanner, Planner                   # noqa: E402

CANNED_PLAN = {
    "summary": "اقرأ ملف الملاحظات",
    "steps": [
        {"action": "read_file", "params": {"path": "C:\\Users\\me\\notes.txt"},
         "description": "قراءة الملف"}
    ],
}


class MockOllama(QuietHandler):
    canned: dict = CANNED_PLAN
    models: list = ["qwen2.5:3b", "llama3.2:3b"]
    last_request: dict = {}

    def do_GET(self) -> None:               # noqa: N802
        if self.path == "/api/tags":
            self._send({"models": [{"name": m} for m in self.models]})
        else:
            self.send_error(404)

    def do_POST(self) -> None:              # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        MockOllama.last_request = json.loads(self.rfile.read(length) or b"{}")
        self._send({
            "message": {"role": "assistant",
                        "content": json.dumps(self.canned, ensure_ascii=False)}
        })

    def _send(self, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.write_safely(data)


class LocalPlannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = start_server(MockOllama)
        cls.port = cls.server.server_address[1]
        cls.url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls) -> None:
        stop_server(cls.server)

    def planner(self, model="qwen2.5:3b", url=None) -> LocalPlanner:
        return LocalPlanner(url or self.url, model, timeout=10)

    def test_status_reports_ready(self):
        status = self.planner().status()
        self.assertTrue(status.reachable)
        self.assertIn("ready", status.detail.lower())
        self.assertIn("qwen2.5:3b", status.models)

    def test_missing_model_is_reported(self):
        status = self.planner(model="ghost:1b").status()
        self.assertTrue(status.reachable)
        self.assertIn("not installed", status.detail)
        self.assertIn("ollama pull ghost:1b", status.detail)

    def test_plan_uses_model_output(self):
        plan = self.planner().plan("اقرأ لي ملف الملاحظات", PROJECT_DIR, 6)
        self.assertEqual(plan.planner, "local")
        self.assertEqual(plan.model, "qwen2.5:3b")
        self.assertEqual(plan.summary, "اقرأ ملف الملاحظات")
        self.assertEqual(plan.steps[0].action, "read_file")
        self.assertEqual(plan.steps[0].params["path"], "C:\\Users\\me\\notes.txt")
        self.assertEqual(plan.steps[0].description, "قراءة الملف")
        self.assertIn("Ollama", plan.label())

    def test_request_asks_for_json_and_contains_context(self):
        self.planner().plan("take a screenshot", PROJECT_DIR, 6)
        sent = MockOllama.last_request
        self.assertEqual(sent.get("format"), "json")
        self.assertFalse(sent.get("stream"))
        self.assertEqual(sent.get("model"), "qwen2.5:3b")
        messages = sent["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("NEVER invent", messages[0]["content"])
        user_content = messages[-1]["content"]
        self.assertIn("take a screenshot", user_content)
        self.assertIn("Operating system", user_content)

    def test_unreachable_ollama_raises(self):
        from assistant.planner import PlannerError

        with self.assertRaises(PlannerError):
            self.planner(url="http://127.0.0.1:9").plan("x", PROJECT_DIR, 6)

    def test_facade_local_mode_falls_back_to_keywords(self):
        planner = Planner(mode="local", local=self.planner(url="http://127.0.0.1:9"),
                          workspace_root=PROJECT_DIR)
        plan = planner.plan("خد لقطة شاشة")
        self.assertEqual(plan.planner, "keywords")
        self.assertEqual(plan.steps[0].action, "screenshot")
        self.assertIn("local:", plan.note)

    def test_facade_local_mode_uses_ollama_when_up(self):
        planner = Planner(mode="local", local=self.planner(),
                          workspace_root=PROJECT_DIR)
        plan = planner.plan("اقرأ لي ملف الملاحظات")
        self.assertEqual(plan.planner, "local")

    def test_model_missing_in_ollama_falls_back(self):
        planner = Planner(mode="local", local=self.planner(model="ghost:1b"),
                          workspace_root=PROJECT_DIR)
        # The mock still answers /api/chat, so the plan succeeds;
        # this documents that availability is reported separately.
        status = self.planner(model="ghost:1b").status()
        self.assertIn("not installed", status.detail)
        self.assertEqual(planner.chain(), ["local", "keywords"])

    def test_raw_bang_command_skips_every_model(self):
        planner = Planner(mode="local", local=self.planner(),
                          workspace_root=PROJECT_DIR)
        before = dict(MockOllama.last_request)
        plan = planner.plan("!Get-Date")
        self.assertEqual(plan.steps[0].action, "powershell")
        self.assertEqual(plan.steps[0].params["command"], "Get-Date")
        self.assertEqual(MockOllama.last_request, before,
                         "the ! prefix must not call the model")


if __name__ == "__main__":
    unittest.main(verbosity=2)
