"""Static safety checks for the Windows .bat launchers.

install.bat crashed after step 3 with ". was unexpected at this time.":
an ``echo`` line inside a parenthesized ``if/else`` block contained an
unescaped ``)`` (``echo [4/6] Created config.ini (allowed user id: ...).``).
Inside a ``( )`` block cmd closes the block at the first unescaped ``)``,
even inside double quotes, so these checks parse every .bat file the same
way cmd does and refuse unescaped parentheses inside ``echo`` lines.

These tests run everywhere (no Windows needed) and would have caught the
crash before release.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
BAT_FILES = sorted(PROJECT_DIR.glob("*.bat"))

# ``echo.`` prints an empty line; otherwise everything after "echo" is text.
ECHO_RE = re.compile(r"^\s*@?echo(?P<dot>\.)?(?:\s+(?P<text>.*))?\s*$")
# ``^)`` / ``^(`` are escaped for cmd; strip the caret before looking for parens.
CARET_ESCAPE_RE = re.compile(r"\^.")


def echo_bugs(lines: list[str]) -> list[str]:
    """Return descriptions of unescaped parentheses in echos inside ( ) blocks.

    The block parser mirrors cmd: a line ending in ``(`` opens a block, a
    line starting with ``)`` closes one (``) else (`` does both). Quotes do
    NOT protect parentheses from the block parser.
    """
    bugs: list[str] = []
    depth = 0
    for lineno, raw in enumerate(lines, start=1):
        line = raw.rstrip("\r\n").strip()
        closes = line.startswith(")")
        opens = line.endswith("(")
        inside_block = (depth - (1 if closes else 0)) > 0
        if inside_block:
            match = ECHO_RE.match(line)
            if match and match.group("text"):
                leftover = CARET_ESCAPE_RE.sub("", match.group("text"))
                if "(" in leftover or ")" in leftover:
                    bugs.append(
                        f"line {lineno}: echo inside a ( ) block has unescaped "
                        f"parentheses: {line.strip()}"
                    )
        depth += (1 if opens else 0) - (1 if closes else 0)
        depth = max(depth, 0)
    return bugs


class BatchFileTests(unittest.TestCase):
    def test_bat_files_found(self):
        self.assertTrue(
            BAT_FILES, "expected the launcher .bat files next to the tests"
        )

    def test_crlf_line_endings(self):
        """cmd.exe mishandles LF-only files (labels/blocks), so keep CRLF."""
        for path in BAT_FILES:
            raw = path.read_bytes()
            self.assertEqual(
                raw.count(b"\n"), raw.count(b"\r\n"),
                f"{path.name} must use CRLF line endings",
            )

    def test_no_unescaped_parens_in_echo_inside_blocks(self):
        found: list[str] = []
        for path in BAT_FILES:
            text = path.read_bytes().decode("utf-8", errors="replace")
            found += [f"{path.name}: {bug}" for bug in echo_bugs(text.split("\n"))]
        self.assertEqual(found, [], "\n".join(found))

    def test_parser_catches_the_original_install_crash(self):
        """Control: the exact line that crashed install.bat must be flagged."""
        script = (
            'if exist "config.ini" (\r\n'
            "    echo [4/6] Created config.ini (allowed user id: 7823157087).\r\n"
            ")\r\n"
        )
        bugs = echo_bugs(script.split("\n"))
        self.assertEqual(len(bugs), 1, bugs)

    def test_escaped_parens_are_accepted(self):
        script = (
            'if exist "config.ini" (\r\n'
            "    echo [4/6] Created config.ini ^(allowed user id: 1^).\r\n"
            ")\r\n"
        )
        self.assertEqual(echo_bugs(script.split("\n")), [])

    def test_install_bat_reads_input_eof_safely(self):
        """install.bat must not hang when run with stdin closed.

        ``choice`` can wait forever without a console; ``set /p`` simply
        continues on EOF, so interactive reads must use ``set /p``.
        """
        raw = (PROJECT_DIR / "install.bat").read_bytes().decode("utf-8")
        # A `choice` *command* (line starting with it) can wait forever without
        # a console; comments mentioning the word are fine.
        self.assertIsNone(re.search(r"(?im)^\s*@?choice\b", raw))
        self.assertIn("set /p", raw.lower())


class InstallerFlowTests(unittest.TestCase):
    """Control flow of install.bat: every jump has a target, and no exit is silent."""

    def _lines(self) -> list[str]:
        return (PROJECT_DIR / "install.bat").read_text(encoding="utf-8").splitlines()

    def test_every_goto_target_exists(self) -> None:
        lines = self._lines()
        labels = {line.strip()[1:].strip().lower() for line in lines if line.strip().startswith(":")}
        targets = set()
        for line in lines:
            for match in re.finditer(r"goto\s+(\S+)", line, re.IGNORECASE):
                targets.add(match.group(1).lower())
        self.assertTrue(targets, "install.bat should use goto")
        missing = sorted(t for t in targets if t != ":eof" and t not in labels)
        self.assertEqual(missing, [], f"goto targets without a label: {missing}")

    def test_every_exit_is_preceded_by_a_pause(self) -> None:
        lines = [line.strip().lower() for line in self._lines()]
        exits = [i for i, line in enumerate(lines) if line.startswith("exit /b")]
        self.assertTrue(exits)
        for i in exits:
            previous = lines[max(0, i - 3):i]
            self.assertTrue(any(line == "pause" for line in previous),
                            f"exit at line {i + 1} is not preceded by pause")

    def test_no_python_3_10_requirement_anywhere(self) -> None:
        for path in BAT_FILES:
            self.assertNotIn("3.10", path.read_text(encoding="utf-8", errors="replace"), path.name)

    def test_every_error_block_reaches_a_pause(self) -> None:
        """Each block that prints an [X] error must lead, by goto, to a pause before exit."""
        lines = [line.strip() for line in self._lines()]
        labels = {line[1:].strip().lower(): i for i, line in enumerate(lines) if line.startswith(":")}
        error_labels = []
        for name, start in labels.items():
            block = lines[start:start + 40]
            if any(line.startswith("echo") and "[X]" in line for line in block):
                error_labels.append(name)
        self.assertTrue(error_labels, "install.bat should have error blocks")
        for name in error_labels:
            index, hops, reached = labels[name], 0, False
            while hops < 30:
                hops += 1
                line = lines[index].lower()
                if line == "pause":
                    reached = True
                    break
                if line.startswith("exit"):
                    break
                jump = re.match(r"goto\s+(\S+)", line)
                if jump and jump.group(1) != ":eof":
                    target = jump.group(1).lstrip(":")
                    self.assertIn(target, labels, f"{name} jumps to a missing label {target}")
                    index = labels[target]
                    continue
                index += 1
            self.assertTrue(reached, f"error block :{name} does not reach pause")

    def test_failed_token_save_is_not_reported_as_saved(self) -> None:
        lines = [line.strip().lower() for line in self._lines()]
        call = next(i for i, line in enumerate(lines) if "--set-token" in line)
        guard = next(i for i in range(call, call + 3) if lines[i] == "if errorlevel 1 goto token_not_saved")
        stored = next(i for i in range(call, call + 6) if lines[i] == 'set "stored=1"')
        self.assertLess(guard, stored, "STORED may only be set after the save succeeded")

    def test_python_failure_paths_print_the_download_link(self) -> None:
        text = (PROJECT_DIR / "install.bat").read_text(encoding="utf-8")
        for label in (":no_python", ":old_python"):
            match = re.search(rf"^{re.escape(label)}\s*$", text, re.M)
            self.assertIsNotNone(match, label)
            start = match.start()
            block = text[start:start + 1200]
            self.assertIn("https://www.python.org/downloads/windows/", block, label)
            self.assertIn("Add python.exe to PATH", block, label)
