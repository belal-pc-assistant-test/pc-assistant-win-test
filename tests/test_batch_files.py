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
