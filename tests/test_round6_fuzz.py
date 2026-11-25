"""Round 6 fuzz and property tests for the plan validator and the executor.

Seeded, so every run is the same. Properties checked:

* ``build_plan`` either returns a valid plan or raises ``PlanValidationError``.
  No other exception type may escape, whatever the model text looks like.
* A returned plan only uses known actions and their exact parameter names,
  respects the step limit, and can be hashed for approval.
* Building or sanitizing a plan never touches the filesystem: nothing runs
  before approval.
* The executor never raises, and it never reads or writes outside the allowed
  folder, even for traversal, absolute, device and protected-name paths.
"""

from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path

from _bootstrap import PROJECT_DIR
assert PROJECT_DIR.is_dir()

from assistant import audit  # noqa: E402
from assistant.action_spec import PARAM_SPEC, VALID_ACTIONS  # noqa: E402
from assistant.actions import base, dispatch  # noqa: E402
from assistant.plan_code import fingerprint  # noqa: E402
from assistant.planner import (  # noqa: E402
    PlanValidationError,
    Step,
    build_plan,
    sanitize_and_annotate,
)
from test_flow import make_config  # noqa: E402

TEXTS = [
    "", "a", "مرحبا بالعالم", "\ud800", "\udc80 lone", "x" * 5000, "../../etc/passwd",
    "..\\..\\token.txt", "C:\\Windows\\System32", "\\\\server\\share\\x", "a\x00b", "CON",
    "token.txt.", "token.txt ", "TOKEN~1.TXT", "🙂 emoji", "\"quote\"", "line\nbreak", "\\",
    "'", "<b>x</b>", "${HOME}", "%PATH%", "`cmd`", "; rm -rf /", "&& calc", "COM\u00b9.txt",
    "file.txt:stream", "/tmp/outside/x.txt", "C:/Users/أحمد/مستندات/ملف.txt", "  ", "\t\r\n",
]
KEYS = ["path", "content", "text", "command", "when", "name", "url", "minutes", "destination",
        "x", "query", "file", "action", "steps", "summary"]
NUMBERS = [0, -1, 1, 2 ** 70, 1.5, -0.0, float("1e308")]


def rand_value(rng: random.Random, depth: int = 0):
    roll = rng.random()
    if depth > 3 or roll < 0.6:
        return rng.choice(TEXTS + NUMBERS + [True, False, None])
    if roll < 0.8:
        return [rand_value(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    return {rng.choice(KEYS): rand_value(rng, depth + 1) for _ in range(rng.randint(0, 3))}


def rand_step(rng: random.Random):
    action = rng.choice(list(VALID_ACTIONS) + [
        "unknown_action", "", "remote_start", "shutdown", "powershell", "rm", None, 42, ["reply"],
    ])
    params = {}
    for key in rng.sample(KEYS, k=rng.randint(0, 4)):
        params[key] = rand_value(rng)
    if isinstance(action, str) and action in PARAM_SPEC and rng.random() < 0.6:
        # Mostly well-formed parameters, so the valid path is exercised too.
        allowed = PARAM_SPEC[action]
        for name in list(allowed)[:2]:
            params[name] = rng.choice(TEXTS[:12] + ["C:\\ok\\file.txt", "hello"])
    step = {"action": action, "params": params}
    if rng.random() < 0.3:
        step["description"] = rng.choice(TEXTS)
    return step


def rand_raw(rng: random.Random) -> str:
    roll = rng.random()
    if roll < 0.08:
        return "".join(rng.choice("{}[]\":,0123456789 truefalsnul") for _ in range(rng.randint(0, 300)))
    if roll < 0.12:
        depth = rng.randint(50, 3000)
        return rng.choice(["[" * depth, '{"a":' * depth, "[" * depth + "]" * depth])
    body = {
        "summary": rng.choice(TEXTS + [None, 3]),
        "steps": [rand_step(rng) for _ in range(rng.randint(0, 9))],
    }
    text = json.dumps(body, ensure_ascii=rng.random() < 0.5)
    mutation = rng.random()
    if mutation < 0.1:
        text = text[: rng.randint(0, len(text))]           # truncated reply
    elif mutation < 0.2:
        text = "```json\n" + text + "\n```"                   # fenced reply
    elif mutation < 0.25:
        text = "Sure! " + text + " Anything else?"           # chatty reply
    return text


def snapshot(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


class PlanValidatorFuzzTests(unittest.TestCase):

    ITERATIONS = 700

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sandbox = self.root / "sandbox"
        self.sandbox.mkdir()
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self.secret = self.outside / "secret.txt"
        self.secret.write_text("SECRET-OUTSIDE", encoding="utf-8")
        audit.setup(self.root / "logs" / "a.log", "CRITICAL")

    def tearDown(self):
        audit.close()
        self.tmp.cleanup()

    def test_random_model_replies_only_yield_valid_plans_or_validation_errors(self):
        rng = random.Random(20261125)
        valid = 0
        before = snapshot(self.root)
        for _ in range(self.ITERATIONS):
            raw = rand_raw(rng)
            max_steps = rng.choice([1, 3, 6])
            try:
                plan = build_plan(raw, "cloud", "m", max_steps, self.sandbox)
            except PlanValidationError:
                continue
            except Exception as exc:  # any other type is a bug
                self.fail(f"{type(exc).__name__} escaped build_plan for {raw[:160]!r}: {exc}")
            valid += 1
            self.assertLessEqual(len(plan.steps), max_steps)
            for step in plan.steps:
                self.assertIn(step.action, VALID_ACTIONS)
                spec = PARAM_SPEC[step.action]
                allowed = set(spec["required"]) | set(spec["optional"])
                self.assertTrue(set(step.params) <= allowed,
                                f"unexpected params for {step.action}: {set(step.params)}")
                self.assertTrue(set(spec["required"]) <= set(step.params),
                                f"missing required params for {step.action}")
            fingerprint(plan.steps)  # must hash cleanly (lone surrogates are refused earlier)
            for step in plan.steps:
                for value in step.params.values():
                    if isinstance(value, str):
                        value.encode("utf-8")
        self.assertGreater(valid, 0, "the generator should produce some valid plans")
        self.assertEqual(snapshot(self.root), before, "building plans must not touch the filesystem")

    def test_sanitizer_never_raises_on_any_path_text(self):
        rng = random.Random(7)
        codepoints = [chr(c) for c in range(0, 0x250)] + ["\ud800", "\udfff", "\x00", "\\\\?\\", "\\??\\"]
        before = snapshot(self.root)
        for _ in range(400):
            raw = "".join(rng.choice(codepoints) for _ in range(rng.randint(0, 40)))
            steps = [Step("read_file", {"path": raw}), Step("send_file", {"path": raw})]
            sanitize_and_annotate(steps, self.sandbox)  # must not raise
        self.assertEqual(snapshot(self.root), before, "sanitizing must not touch the filesystem")

    def test_path_syntax_check_only_raises_action_errors(self):
        rng = random.Random(11)
        codepoints = list("abc\\/:.~ $") + ["\x00", "\u00b9", "\ud800", "أ"]
        for _ in range(1000):
            raw = "".join(rng.choice(codepoints) for _ in range(rng.randint(0, 24)))
            try:
                base.check_path_syntax(raw)
            except base.ActionError:
                pass
            except Exception as exc:
                self.fail(f"{type(exc).__name__} for path {raw!r}: {exc}")


class ExecutorFuzzTests(unittest.TestCase):

    ITERATIONS = 250

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sandbox = self.root / "sandbox"
        self.sandbox.mkdir()
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self.secret = self.outside / "secret.txt"
        self.secret.write_text("SECRET-OUTSIDE", encoding="utf-8")
        audit.setup(self.root / "logs" / "a.log", "CRITICAL")
        self.config = make_config(self.root)
        self.config.allowed_roots = [self.sandbox]

    def tearDown(self):
        audit.close()
        self.tmp.cleanup()

    def test_executor_never_raises_and_stays_inside_allowed_folder(self):
        rng = random.Random(4242)
        paths = [
            "../outside/x.txt", str(self.outside / "x.txt"), "..\\outside\\x.txt",
            str(self.secret), "secret.txt", "token.txt", "TOKEN.TXT", "token.txt.",
            "TOKEN~1.TXT", "CON", "COM\u00b9.txt", "C:\\Windows\\win.ini", "\\\\server\\share\\x",
            "notes\x00.txt", "x.txt:stream", "مرحبا.txt", "a" * 400 + ".txt", "",
            str(self.sandbox / "ok.txt"), "sub/../../outside/y.txt",
        ]
        for _ in range(self.ITERATIONS):
            path = rng.choice(paths + [rand_value_text(rng)])
            action = rng.choice(["write_file", "read_file"])
            params = {"path": path}
            if action == "write_file":
                params["content"] = rng.choice(TEXTS[:20] + ["payload"])
            step = Step(action, params)
            try:
                result = dispatch.execute_step(step, self.config)
            except Exception as exc:
                self.fail(f"execute_step raised {type(exc).__name__} for {path!r}: {exc}")
            self.assertNotIn("SECRET-OUTSIDE", result.output, f"read outside the sandbox via {path!r}")
        self.assertEqual(self.secret.read_text(encoding="utf-8"), "SECRET-OUTSIDE")
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ["secret.txt"],
                         "nothing may be written outside the allowed folder")
        self.assertFalse((self.sandbox / "token.txt").exists(), "protected names are never written")


def rand_value_text(rng: random.Random) -> str:
    parts = ["..", "outside", "x", "\\", "/", "C:", " ", "é", "ملف", ".txt", "\u00b9"]
    return "".join(rng.choice(parts) for _ in range(rng.randint(1, 8)))


if __name__ == "__main__":
    unittest.main()
