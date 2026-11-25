"""Regression tests for Windows-specific behaviour that Linux cannot show.

Covers three real windows-latest failures:

1. 8.3 short names: ``C:\\Users\\RUNNER~1\\…`` and ``C:\\Users\\runneradmin\\…``
   are the same folder; un-normalized allow-list checks rejected one of the
   two spellings ("outside the allowed folders") depending on which form the
   path happened to arrive in.
2. Audit log handle leak: Windows cannot delete an open file (WinError 32),
   so ``audit.close()`` must release every handler (used on shutdown and in
   test tearDowns).
3. Console encoding: entry points must reconfigure stdout/stderr to UTF-8
   with ``errors=replace`` before printing emoji.

The 8.3 expansion is simulated by patching ``assistant.config`` — the real
``GetLongPathNameW`` call only exists on Windows — so these tests catch the
comparison logic bug on every OS.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

from assistant import audit
from assistant.actions import execute_step
from assistant.actions.base import ensure_inside
from assistant.config import normalize_path, path_is_inside
from assistant.planner import Step

from test_assistant import make_config


SHORT_TO_LONG = {"adminho~1": "adminhome", "runner~1": "runneradmin"}


def fake_expand_short_names(path: Path) -> Path:
    """Simulate GetLongPathNameW with a fixed 8.3 component mapping."""
    parts = [SHORT_TO_LONG.get(part.lower(), part) for part in Path(path).parts]
    return Path(*parts) if parts else path


class NormalizePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "adminhome" / "docs").mkdir(parents=True)
        (self.root / "adminhome" / "docs" / "file.txt").write_bytes(b"abc")

    def test_existing_tails_resolve(self):
        self.assertEqual(
            normalize_path(self.root / "adminhome" / "docs"),
            Path(self.tmp.name).resolve() / "adminhome" / "docs",
        )

    def test_nonexistent_tail_keeps_resolved_parent(self):
        target = self.root / "adminhome" / "newdir" / "f.txt"
        self.assertEqual(
            normalize_path(target),
            Path(self.tmp.name).resolve() / "adminhome" / "newdir" / "f.txt",
        )

    def test_windows_case_insensitive_containment(self):
        self.assertTrue(path_is_inside(
            PureWindowsPath("C:/Users/Me/file.txt"), PureWindowsPath("c:/users/me")))
        self.assertTrue(path_is_inside(
            PureWindowsPath("C:/Users/me"), PureWindowsPath("C:/Users/ME/")))
        # A boundary-safe check: C:\Users\Mex is not inside C:\Users\Me.
        self.assertFalse(path_is_inside(
            PureWindowsPath("C:/Users/Mex/f.txt"), PureWindowsPath("c:/users/me")))

    def test_short_name_root_accepts_long_form_path(self):
        """The windows-latest bug: configured root used the 8.3 spelling."""
        cfg = make_config(self.root)
        cfg.allowed_roots = [self.root / "ADMINHO~1" / "docs"]  # 8.3 spelling
        with patch.object(sys.modules["assistant.config"], "IS_WINDOWS", True), \
             patch.object(sys.modules["assistant.config"], "_expand_short_names",
                          fake_expand_short_names):
            result = execute_step(
                Step("send_file", {"path": str(self.root / "adminhome" / "docs" / "file.txt")}),
                cfg,
            )
        self.assertTrue(result.ok, result.output)
        self.assertEqual(result.files, [normalize_path(self.root / "adminhome" / "docs" / "file.txt")])

    def test_long_name_root_accepts_short_form_path(self):
        cfg = make_config(self.root)
        cfg.allowed_roots = [self.root / "adminhome" / "docs"]  # long spelling
        with patch.object(sys.modules["assistant.config"], "IS_WINDOWS", True), \
             patch.object(sys.modules["assistant.config"], "_expand_short_names",
                          fake_expand_short_names):
            normalized = ensure_inside(
                self.root / "ADMINHO~1" / "docs" / "file.txt",
                cfg.allowed_roots,
                "File path",
            )
        self.assertIn("adminhome", str(normalized))
        self.assertNotIn("~1", str(normalized))

    def test_ensure_inside_still_rejects_outside_paths(self):
        cfg = make_config(self.root)
        cfg.allowed_roots = [self.root / "adminhome"]
        with patch.object(sys.modules["assistant.config"], "IS_WINDOWS", True), \
             patch.object(sys.modules["assistant.config"], "_expand_short_names",
                          fake_expand_short_names):
            with self.assertRaises(Exception):
                ensure_inside(self.root / "elsewhere" / "x.txt", cfg.allowed_roots)


class AuditCloseTests(unittest.TestCase):
    def test_close_releases_log_folder(self):
        """tearDown/shutdown pattern: after close(), the folder can be removed.

        On Windows an open file cannot be deleted (WinError 32), which made
        ConfirmationGateTests.test_approve_button_executes fail during
        cleanup. This mirrors that cleanup directly.
        """
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "logs"
            audit.setup(folder / "a.log", "CRITICAL")
            audit.event("probe")
            self.assertTrue(folder.exists())
            audit.close()
            self.assertEqual(audit._LOGGER.handlers, [])
            shutil.rmtree(folder)  # must not raise (Windows file lock)
            audit.setup(folder / "after.log", "CRITICAL")  # re-setup works
            audit.event("second")
            audit.close()

    def test_setup_after_close_writes_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "a.log"
            audit.setup(log, "INFO")
            audit.event("one")
            audit.close()
            audit.setup(log, "INFO")
            audit.event("two")
            audit.close()
            text = log.read_text(encoding="utf-8")
            self.assertIn('"one"', text)
            self.assertIn('"two"', text)


class StdioEncodingTests(unittest.TestCase):
    def test_stdio_is_utf8_after_importing_assistant(self):
        """assistant/__init__ reconfigures stdout/stderr on import."""
        for stream in (sys.stdout, sys.stderr):
            encoding = (getattr(stream, "encoding", "") or "").lower().replace("_", "")
            self.assertEqual(encoding, "utf-8", f"{stream!r} encoding {encoding!r}")


if __name__ == "__main__":
    unittest.main()
