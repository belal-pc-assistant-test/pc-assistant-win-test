"""Round 11 quality tests that run on every platform (hermetic where it matters).

* Process trees: a grandchild that keeps the stdout pipe open must not make
  ``run_process`` hang after a timeout, and /panic (``kill_all``) must reach it.
  These use a real harmless child (a Python sleep), never a shell.
* Non-UTF-8 output from a child must not crash decoding.
* File errors (locked, denied, missing) return a short, readable reason, not a raw
  OS error that carries the full path.
* Escape attempts (``..``, trailing dots and spaces, ``~``, ``%VAR%``, ``\\\\?\\``)
  are refused or resolve inside the allowed folder.
* Clipboard and screenshot failures return a clean message with no traceback.
* The log file: an unopenable log does not stop startup, the token never reaches
  the file, and the file rotates.

The Windows-only counterparts (NTFS, junctions, locked files, ``taskkill``) are in
``test_windows_real.py``.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

from test_flow import make_config  # noqa: E402  (shared fakes; also sets a private token store)
from _hermetic import IsolatedCase, release_log_then_remove  # noqa: E402

from assistant import audit  # noqa: E402
from assistant.actions import base, core as core_mod, system as system_mod  # noqa: E402
from assistant.actions import transfer as transfer_mod  # noqa: E402
from assistant.actions.base import ActionContext, ActionError  # noqa: E402
from assistant.config import path_is_inside  # noqa: E402

TOKEN = "123456789:AAHround11Test_abcdefghijklmnopqrstuvwxyz"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PARENT_SRC = (
    "import subprocess, sys, time\n"
    # The grandchild gets this process's stdout and stderr pipes explicitly. Windows does
    # not pass handles on by default (close_fds), so this is what makes the stray helper
    # hold the pipe open, as a real one does.
    "subprocess.Popen([sys.executable, sys.argv[2], sys.argv[1]], stdout=sys.stdout, stderr=sys.stderr)\n"
    "time.sleep(90)\n"
)
GRANDCHILD_SRC = (
    "import sys, time\n"
    "deadline = time.time() + 90\n"
    "with open(sys.argv[1], 'a', encoding='utf-8') as fh:\n"
    "    while time.time() < deadline:\n"
    "        fh.write('x'); fh.flush(); time.sleep(0.1)\n"
)


def wait_for(predicate, seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def assert_stopped(test: unittest.TestCase, path: Path) -> None:
    """The heartbeat file must stop growing once the process tree is dead."""
    time.sleep(0.5)
    settled = path.stat().st_size
    time.sleep(1.5)
    test.assertEqual(path.stat().st_size, settled, "a process was still writing after the kill")


class _Workspace(IsolatedCase):
    """A workspace with one allowed folder, plus a secret file outside it."""

    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.config = make_config(self.dir)
        self.ws = self.dir / "ws"
        self.ws.mkdir()
        self.outside = self.dir / "outside"
        self.outside.mkdir()
        self.secret = self.outside / "secret.txt"
        self.secret.write_text("TOP SECRET CONTENT", encoding="utf-8")
        (self.ws / "notes.txt").write_text("hello notes", encoding="utf-8")
        self.config.allowed_roots = [self.ws]
        self.config.workspace_root = self.ws
        self.config.downloads_dir = self.ws / "downloads"

    def ctx(self) -> ActionContext:
        return ActionContext(config=self.config)


# ---------------------------------------------------------------------------
# 1. Process trees and non-UTF-8 output
# ---------------------------------------------------------------------------

class ProcessTreeTests(unittest.TestCase):

    def setUp(self) -> None:
        base.clear_abort()
        self.addCleanup(base.clear_abort)
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.heartbeat = self.dir / "heartbeat.txt"
        (self.dir / "parent.py").write_text(PARENT_SRC, encoding="utf-8")
        (self.dir / "grand.py").write_text(GRANDCHILD_SRC, encoding="utf-8")
        self.argv = [sys.executable, str(self.dir / "parent.py"), str(self.heartbeat),
                     str(self.dir / "grand.py")]

    def _grandchild_started(self) -> bool:
        return self.heartbeat.exists() and self.heartbeat.stat().st_size > 0

    def test_timeout_returns_and_kills_the_grandchild_too(self):
        """Regression: an unbounded communicate() after kill() hung when a grandchild held the pipe."""
        outcome: dict[str, object] = {}

        def run() -> None:
            try:
                base.run_process(self.argv, timeout=3)
            except ActionError as exc:
                outcome["error"] = exc
            except Exception as exc:  # pragma: no cover - reported below
                outcome["other"] = exc

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        self.assertTrue(wait_for(self._grandchild_started, 60), "the grandchild never started")
        worker.join(60)
        self.assertFalse(worker.is_alive(),
                         "run_process hung after the timeout: a grandchild kept the pipe open")
        self.assertNotIn("other", outcome, outcome.get("other"))
        self.assertIn("timed out", str(outcome.get("error")))
        assert_stopped(self, self.heartbeat)

    def test_kill_all_stops_the_whole_tree(self):
        """/panic (ProcessRegistry.kill_all) must reach the grandchild, not only the direct child."""
        kwargs = {} if base.IS_WINDOWS else {"start_new_session": True}
        proc = subprocess.Popen(self.argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", **kwargs)
        base.REGISTRY.add(proc)
        self.addCleanup(base.REGISTRY.discard, proc)
        self.addCleanup(self._reap, proc)
        self.assertTrue(wait_for(self._grandchild_started, 60), "the grandchild never started")

        killed = base.REGISTRY.kill_all()

        self.assertGreaterEqual(killed, 1)
        self.assertIsNotNone(proc.wait(timeout=30))
        assert_stopped(self, self.heartbeat)

    @staticmethod
    def _reap(proc: subprocess.Popen) -> None:
        try:
            proc.communicate(timeout=10)
        except Exception:
            proc.kill()

    def test_non_utf8_output_does_not_crash_decoding(self):
        """Code-page bytes (for example OEM 866 or cp1256) must decode with replacement."""
        code = ("import sys; sys.stdout.buffer.write(b'\\x8d\\xa0\\xff ok'); "
                "sys.stderr.buffer.write(b'\\x82\\xfe')")
        proc = base.run_process([sys.executable, "-c", code], timeout=60)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("ok", proc.stdout)
        self.assertIn("\ufffd", proc.stdout)


# ---------------------------------------------------------------------------
# 2. File errors are readable (no raw OS error with the full path)
# ---------------------------------------------------------------------------

class FileErrorTests(_Workspace):

    def test_locked_or_denied_read_gives_a_short_reason(self):
        target = self.ws / "notes.txt"
        error = PermissionError(13, "Permission denied", str(target))
        with mock.patch.object(core_mod, "read_text_file", side_effect=error):
            with self.assertRaises(ActionError) as caught:
                core_mod.read_file({"path": str(target)}, self.ctx())
        message = str(caught.exception)
        self.assertIn("access was denied", message)
        self.assertNotIn("Errno", message)
        self.assertNotIn(str(self.ws), message, "the raw path must not reach the user")

    def test_missing_file_still_says_not_found(self):
        with self.assertRaises(ActionError) as caught:
            core_mod.read_file({"path": str(self.ws / "missing.txt")}, self.ctx())
        self.assertIn("File not found", str(caught.exception))

    def test_locked_file_in_send_file_gives_a_short_reason(self):
        target = self.ws / "notes.txt"
        real_stat = Path.stat

        def denied_for_target(path_self, *args, **kwargs):
            if path_self == target:
                raise PermissionError(13, "Permission denied", str(target))
            return real_stat(path_self, *args, **kwargs)

        with mock.patch.object(Path, "stat", denied_for_target):
            with self.assertRaises(ActionError) as caught:
                transfer_mod.send_file({"path": str(target)}, self.ctx())
        self.assertIn("access was denied", str(caught.exception))
        self.assertNotIn("Errno", str(caught.exception))


# ---------------------------------------------------------------------------
# 3. Allowed-folder escape attempts (any platform: refused or inside the root)
# ---------------------------------------------------------------------------

class PathEscapeTests(_Workspace):

    ESCAPES = (
        "../outside/secret.txt",
        "sub/../../outside/secret.txt",
        "notes.txt/../../outside/secret.txt",
        "..\\outside\\secret.txt",
        "sub\\..\\..\\outside/secret.txt",
        "../outside/secret.txt.",
        "../outside/secret.txt ",
        "~/secret.txt",
        "%TPA_UNUSED_VARIABLE%\\secret.txt",
        "\\\\?\\" + "C:\\outside\\secret.txt",
    )

    def _assert_refused_or_inside(self, raw: str) -> None:
        try:
            result = core_mod.read_file({"path": raw}, self.ctx())
        except ActionError:
            return  # refused: the expected outcome
        first_line = result.output.splitlines()[0]
        self.assertTrue(path_is_inside(Path(first_line), self.ws),
                        f"{raw!r} read {first_line!r}, which is outside the allowed folder")
        self.assertNotIn("TOP SECRET CONTENT", result.output)

    def test_escape_attempts_are_refused_or_stay_inside(self):
        for raw in self.ESCAPES:
            with self.subTest(raw=raw):
                self._assert_refused_or_inside(raw)

    def test_a_plain_name_inside_the_folder_still_reads(self):
        result = core_mod.read_file({"path": "notes.txt"}, self.ctx())
        self.assertIn("hello notes", result.output)

    def test_send_file_refuses_the_same_escapes(self):
        for raw in self.ESCAPES:
            with self.subTest(raw=raw):
                try:
                    result = transfer_mod.send_file({"path": raw}, self.ctx())
                except ActionError:
                    continue
                self.assertTrue(all(path_is_inside(p, self.ws) for p in result.files), raw)


# ---------------------------------------------------------------------------
# 4. Clipboard and screenshot errors are clean (stubbed; never touches the real device)
# ---------------------------------------------------------------------------

TRACEBACK_TEXT = (
    'Traceback (most recent call last):\n'
    '  File "C:\\Users\\Alice\\app\\clip.py", line 3, in <module>\n'
    'RuntimeError: clipboard is busy\n'
)


class ClipboardErrorTests(unittest.TestCase):

    def test_posix_clipboard_write_timeout_is_readable(self):
        def fake_which(name):
            return "/usr/bin/xclip" if name == "xclip" else None

        with mock.patch.object(system_mod, "IS_WINDOWS", False), \
                mock.patch.object(system_mod.shutil, "which", side_effect=fake_which), \
                mock.patch.object(system_mod.subprocess, "run",
                                  side_effect=subprocess.TimeoutExpired("xclip", 15)):
            with self.assertRaises(ActionError) as caught:
                system_mod._clipboard_write("hello")
        self.assertIn("did not respond in time", str(caught.exception))

    def test_windows_clipboard_read_failure_has_no_traceback_or_user_path(self):
        failed = subprocess.CompletedProcess([], 1, "", TRACEBACK_TEXT)
        with mock.patch.object(system_mod, "IS_WINDOWS", True), \
                mock.patch.object(system_mod, "run_powershell", return_value=failed):
            with self.assertRaises(ActionError) as caught:
                system_mod._clipboard_read()
        message = str(caught.exception)
        self.assertIn("clipboard", message)
        self.assertNotIn("Traceback", message)
        self.assertNotIn("  File ", message)
        self.assertNotIn("Alice", message)
        self.assertNotIn("C:\\", message)

    def test_windows_clipboard_write_failure_has_no_traceback(self):
        failed = subprocess.CompletedProcess([], 1, "", TRACEBACK_TEXT)
        with mock.patch.object(system_mod, "IS_WINDOWS", True), \
                mock.patch.object(system_mod, "run_powershell", return_value=failed):
            with self.assertRaises(ActionError) as caught:
                system_mod._clipboard_write("hello")
        message = str(caught.exception)
        self.assertNotIn("Traceback", message)
        self.assertNotIn("Alice", message)


class ScreenshotErrorTests(IsolatedCase):

    def test_a_failed_capture_gives_the_fixed_user_message(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        ctx = ActionContext(config=make_config(Path(tmp.name)), capture_allowed=True)
        with mock.patch("PIL.ImageGrab.grab", side_effect=OSError("no display 1 at C:\\Users\\Alice")):
            with self.assertRaises(ActionError) as caught:
                core_mod.take_screenshot({}, ctx)
        self.assertEqual(str(caught.exception), "Screen capture failed. Unlock the desktop and retry.")
        self.assertNotIn("Alice", str(caught.exception))


# ---------------------------------------------------------------------------
# 5. Logging: unopenable log, token never written, rotation
# ---------------------------------------------------------------------------

class LogFileTests(IsolatedCase):

    def setUp(self) -> None:
        super().setUp()
        audit.close()
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(release_log_then_remove, tmp)
        self.dir = Path(tmp.name)
        self.log = self.dir / "logs" / "a.log"

    def test_an_unopenable_log_file_falls_back_to_the_console(self):
        # A folder where the log file should be cannot be opened on any platform.
        blocked = self.dir / "blocked.log"
        blocked.mkdir()
        buffer = StringIO()
        with redirect_stderr(buffer):
            audit.setup(blocked, "INFO")  # must not raise: the bot still has to start
            audit.warning("still running")
        self.assertIn("console only", buffer.getvalue())
        self.assertNotIn(str(blocked), buffer.getvalue(), "the path is not needed in the message")

    def test_the_token_never_reaches_the_log_file(self):
        audit.setup(self.log, "CRITICAL")
        audit.register_secret(TOKEN)
        audit.event("request", reason=f"token {TOKEN}")       # a field: dropped unless a label
        audit.warning(f"message with {TOKEN}")
        try:
            raise RuntimeError(f"upstream said {TOKEN}")
        except RuntimeError:
            audit.error("failed while handling a request", exc_info=True)  # traceback text
        audit.close()
        data = self.log.read_bytes()
        self.assertIn(b'"event": "warning"', data)  # the log was written at all
        self.assertIn(b'"event": "error"', data)
        self.assertNotIn(TOKEN.encode("utf-8"), data)
        self.assertNotIn(TOKEN.split(":", 1)[1].encode("utf-8"), data)

    def test_the_log_rotates_after_two_megabytes(self):
        audit.setup(self.log, "CRITICAL")
        for _ in range(25000):  # each event line is about 110 bytes
            audit.event("rotation", reason="rotation check")
        audit.close()
        rotated = self.log.with_name(self.log.name + ".1")
        self.assertTrue(rotated.exists(), "no rotated file was created")
        self.assertLessEqual(self.log.stat().st_size, 2_000_000)

if __name__ == "__main__":
    unittest.main()
