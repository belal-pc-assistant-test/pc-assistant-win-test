"""Strict plan validation, normalisation and path-hygiene tests."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from assistant.planner import (                                    # noqa: E402
    HAVE_JSONSCHEMA,
    PARAM_SPEC,
    VALID_ACTIONS,
    Plan,
    PlanValidationError,
    Step,
    _validate_builtin,
    _validate_jsonschema,
    build_plan,
    is_placeholder_value,
    normalize_plan_dict,
    plan_json_schema,
    sanitize_and_annotate,
)


class SchemaShapeTests(unittest.TestCase):
    def test_schema_covers_every_action(self):
        schema = plan_json_schema(6)
        variants = schema["properties"]["steps"]["items"]["oneOf"]
        covered = {v["properties"]["action"]["const"] for v in variants}
        self.assertEqual(covered, set(VALID_ACTIONS))

    def test_schema_limits_steps(self):
        self.assertEqual(plan_json_schema(3)["properties"]["steps"]["maxItems"], 3)

    @unittest.skipUnless(HAVE_JSONSCHEMA, "jsonschema not installed")
    def test_schema_is_itself_valid(self):
        import jsonschema

        jsonschema.Draft7Validator.check_schema(plan_json_schema(6))

    @unittest.skipUnless(HAVE_JSONSCHEMA, "jsonschema not installed")
    def test_jsonschema_rejects_wrong_shape(self):
        bad = {"summary": "x", "steps": [{"action": "powershell", "params": {}}]}
        with self.assertRaises(PlanValidationError):
            _validate_jsonschema(bad, 6)


class NormalisationTests(unittest.TestCase):
    def test_valid_plan_normalises_cleanly(self):
        data = {"summary": " ok ", "steps": [
            {"action": "list_dir", "params": {"path": "C:\\Temp"}, "description": " d "}
        ]}
        out = normalize_plan_dict(data, 6)
        self.assertEqual(out["summary"], "ok")
        self.assertEqual(out["steps"][0]["params"]["path"], "C:\\Temp")

    def test_action_aliases(self):
        for alias, expected in [
            ("open_app", "open"), ("run_command", "powershell"), ("ls", "list_dir"),
            ("read", "read_file"), ("write", "write_file"), ("answer", "reply"),
            ("capture_screen", "screenshot"),
        ]:
            data = {"steps": [{"action": alias, "params": {"text": "x", "path": "C:\\a.txt",
                                                           "target": "chrome",
                                                           "command": "Get-Date"}}]}
            out = normalize_plan_dict(data, 6)
            self.assertEqual(out["steps"][0]["action"], expected, alias)

    def test_param_aliases(self):
        data = {"steps": [{"action": "open_app", "app": "chrome"}]}
        out = normalize_plan_dict(data, 6)
        self.assertEqual(out["steps"][0]["params"], {"target": "chrome"})

    def test_top_level_params_are_merged(self):
        data = {"steps": [{"action": "powershell", "command": "Get-Date",
                           "reasoning": "because"}]}
        out = normalize_plan_dict(data, 6)
        self.assertEqual(out["steps"][0]["params"], {"command": "Get-Date"})

    def test_numbers_are_coerced_to_text(self):
        data = {"steps": [{"action": "write_file",
                           "params": {"path": "C:\\a.txt", "content": 12345}}]}
        out = normalize_plan_dict(data, 6)
        self.assertEqual(out["steps"][0]["params"]["content"], "12345")

    def test_extra_params_are_dropped(self):
        data = {"steps": [{"action": "screenshot",
                           "params": {"danger": "rm -rf", "path": "C:\\s.png"}}]}
        out = normalize_plan_dict(data, 6)
        self.assertEqual(out["steps"][0]["params"], {"path": "C:\\s.png"})

    def test_unknown_action_removed_then_error_if_nothing_left(self):
        data = {"steps": [{"action": "delete_everything"}]}
        with self.assertRaises(PlanValidationError):
            normalize_plan_dict(data, 6)

    def test_step_cap_is_enforced(self):
        data = {"steps": [{"action": "screenshot", "params": {}} for _ in range(12)]}
        out = normalize_plan_dict(data, 3)
        self.assertEqual(len(out["steps"]), 3)

    def test_missing_summary_is_generated(self):
        data = {"steps": [{"action": "screenshot", "params": {},
                           "description": "grab the screen"}]}
        out = normalize_plan_dict(data, 6)
        self.assertEqual(out["summary"], "grab the screen")

    def test_bare_action_object_becomes_plan(self):
        data = {"action": "screenshot", "params": {}}
        out = normalize_plan_dict(data, 6)
        self.assertEqual(out["steps"][0]["action"], "screenshot")


class BuiltinValidationTests(unittest.TestCase):
    def good(self) -> dict:
        return {"summary": "s", "steps": [{"action": "screenshot", "params": {}}]}

    def test_accepts_good_plan(self):
        _validate_builtin(self.good(), 6)

    def test_rejects_extra_top_level_key(self):
        plan = self.good()
        plan["explanation"] = "hi"
        with self.assertRaises(PlanValidationError):
            _validate_builtin(plan, 6)

    def test_rejects_empty_summary(self):
        plan = self.good()
        plan["summary"] = "   "
        with self.assertRaises(PlanValidationError):
            _validate_builtin(plan, 6)

    def test_rejects_empty_steps(self):
        with self.assertRaises(PlanValidationError):
            _validate_builtin({"summary": "s", "steps": []}, 6)

    def test_rejects_too_many_steps(self):
        plan = {"summary": "s",
                "steps": [{"action": "screenshot", "params": {}} for _ in range(7)]}
        with self.assertRaises(PlanValidationError):
            _validate_builtin(plan, 6)

    def test_rejects_missing_required_param(self):
        plan = {"summary": "s", "steps": [{"action": "powershell", "params": {}}]}
        with self.assertRaises(PlanValidationError):
            _validate_builtin(plan, 6)

    def test_rejects_empty_required_param(self):
        plan = {"summary": "s",
                "steps": [{"action": "read_file", "params": {"path": "   "}}]}
        with self.assertRaises(PlanValidationError):
            _validate_builtin(plan, 6)

    def test_rejects_non_string_param(self):
        plan = {"summary": "s",
                "steps": [{"action": "read_file", "params": {"path": 5}}]}
        with self.assertRaises(PlanValidationError):
            _validate_builtin(plan, 6)

    def test_rejects_unknown_param(self):
        plan = {"summary": "s",
                "steps": [{"action": "read_file", "params": {"path": "C:\\a.txt",
                                                             "sudo": "yes"}}]}
        with self.assertRaises(PlanValidationError):
            _validate_builtin(plan, 6)

    def test_rejects_oversized_param(self):
        plan = {"summary": "s",
                "steps": [{"action": "write_file",
                           "params": {"path": "C:\\a.txt", "content": "x" * 25000}}]}
        with self.assertRaises(PlanValidationError):
            _validate_builtin(plan, 6)

    def test_rejects_unknown_action(self):
        plan = {"summary": "s", "steps": [{"action": "format_disk", "params": {}}]}
        with self.assertRaises(PlanValidationError):
            _validate_builtin(plan, 6)

    def test_every_action_has_a_param_spec(self):
        # Round 9: the bot-only winget actions have a spec too, but the planner never sees them.
        self.assertEqual(set(PARAM_SPEC), set(VALID_ACTIONS) | {'app_install', 'app_uninstall'})


class PathHygieneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_placeholder_detection(self):
        for value in ["C:\\path\\to\\file.txt", "<path>", "…", "/path/to/x",
                      "%USERPROFILE%\\path", "{file}", "your_file.txt", "placeholder"]:
            self.assertTrue(is_placeholder_value(value), value)
        for value in ["C:\\Users\\Me\\Desktop\\notes.txt", "chrome",
                      "https://example.com/x", "notepad"]:
            self.assertFalse(is_placeholder_value(value), value)

    def test_placeholder_read_step_is_dropped(self):
        steps = [Step("read_file", {"path": "C:\\path\\to\\file.txt"})]
        kept, notes = sanitize_and_annotate(steps, self.ws)
        self.assertEqual(kept[0].action, "reply")
        self.assertTrue(any("placeholder" in n for n in notes))

    def test_placeholder_write_step_is_dropped(self):
        steps = [Step("write_file", {"path": "<path>", "content": "x"})]
        kept, _ = sanitize_and_annotate(steps, self.ws)
        self.assertEqual(kept[0].action, "reply")

    def test_missing_read_path_gets_a_warning_not_a_drop(self):
        target = self.ws / "missing.txt"
        steps = [Step("read_file", {"path": str(target)})]
        kept, _ = sanitize_and_annotate(steps, self.ws)
        self.assertEqual(kept[0].action, "read_file")
        self.assertIn("does not exist", kept[0].warning)

    def test_existing_read_path_has_no_warning(self):
        target = self.ws / "there.txt"
        target.write_text("x", encoding="utf-8")
        steps = [Step("read_file", {"path": str(target)})]
        kept, _ = sanitize_and_annotate(steps, self.ws)
        self.assertEqual(kept[0].warning, "")

    def test_write_into_new_folder_notes_creation(self):
        steps = [Step("write_file", {"path": str(self.ws / "new" / "a.txt"), "content": "x"})]
        kept, _ = sanitize_and_annotate(steps, self.ws)
        self.assertIn("will be created", kept[0].warning)

    def test_urls_are_not_treated_as_paths(self):
        steps = [Step("open", {"target": "https://example.com/a/b"})]
        kept, _ = sanitize_and_annotate(steps, self.ws)
        self.assertEqual(kept[0].action, "open")
        self.assertEqual(kept[0].warning, "")


class BuildPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_model_output(self):
        raw = json.dumps({
            "summary": "List the Desktop",
            "steps": [{"action": "list_dir", "params": {"path": "C:\\Users\\me\\Desktop"},
                       "description": "list desktop"}],
        })
        plan = build_plan(raw, "cloud", "openai", 6, self.ws)
        self.assertIsInstance(plan, Plan)
        self.assertEqual(plan.planner, "cloud")
        self.assertEqual(plan.model, "openai")
        self.assertEqual(plan.steps[0].action, "list_dir")
        self.assertIn("cloud", plan.label())
        self.assertIn("pollinations", plan.label())

    def test_markdown_fence_is_tolerated(self):
        raw = '```json\n{"summary":"s","steps":[{"action":"screenshot","params":{}}]}\n```'
        plan = build_plan(raw, "cloud", "openai", 6, self.ws)
        self.assertEqual(plan.steps[0].action, "screenshot")

    def test_prose_around_json_is_tolerated(self):
        raw = 'Here is the plan: {"summary":"s","steps":[{"action":"screenshot","params":{}}]} thanks'
        plan = build_plan(raw, "local", "qwen2.5:3b", 6, self.ws)
        self.assertEqual(plan.steps[0].action, "screenshot")
        self.assertIn("Ollama", plan.label())

    def test_no_json_raises(self):
        with self.assertRaises(PlanValidationError):
            build_plan("I cannot help with that.", "cloud", "openai", 6, self.ws)

    def test_broken_json_raises(self):
        with self.assertRaises(PlanValidationError):
            build_plan("{not json at all", "cloud", "openai", 6, self.ws)

    def test_missing_required_param_raises(self):
        raw = '{"summary":"s","steps":[{"action":"read_file","params":{}}]}'
        with self.assertRaises(PlanValidationError):
            build_plan(raw, "cloud", "openai", 6, self.ws)

    def test_reply_only_plan_needs_no_action(self):
        raw = '{"summary":"s","steps":[{"action":"reply","params":{"text":"Which file?"}}]}'
        plan = build_plan(raw, "cloud", "openai", 6, self.ws)
        self.assertFalse(plan.needs_action)
        self.assertEqual(plan.reply_text, "Which file?")

    def test_invented_path_is_replaced_by_a_question(self):
        raw = '{"summary":"s","steps":[{"action":"read_file","params":{"path":"C:\\\\path\\\\to\\\\notes.txt"}}]}'
        plan = build_plan(raw, "cloud", "openai", 6, self.ws)
        self.assertEqual(plan.steps[0].action, "reply")
        self.assertIn("path", plan.steps[0].params["text"].lower())

    def test_prompt_contains_key_rules(self):
        from assistant.planner import FEW_SHOT, SYSTEM_PROMPT

        self.assertIn("NEVER invent", SYSTEM_PROMPT)
        self.assertIn("reply", SYSTEM_PROMPT)
        self.assertIn("Arabic", SYSTEM_PROMPT)
        self.assertGreaterEqual(len(FEW_SHOT), 6)
        joined = json.dumps(FEW_SHOT, ensure_ascii=False)
        self.assertIn("لقطة شاشة", joined)          # arabic example present
        self.assertIn("read my notes file", joined)  # "ask, don't invent" example


if __name__ == "__main__":
    unittest.main(verbosity=2)
