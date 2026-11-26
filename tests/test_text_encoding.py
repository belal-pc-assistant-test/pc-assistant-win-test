"""Text encoding and batch-file safety, checked statically on every platform.

Why this exists: on Windows the default text encoding is the locale code page (cp1252 on
the CI runner). Reading UTF-8 Arabic text without an explicit encoding raises
UnicodeDecodeError there, even though the same test passes on Linux (UTF-8 default).

Checks:
  1. Every text-mode open / read_text / write_text / text=True subprocess call in the
     project (code and tests) passes an explicit encoding.
  2. Every .bat file is UTF-8 without a BOM, uses CRLF only, and runs `chcp 65001`
     before its first non-ASCII byte. cmd reads a batch file line by line with the
     active code page, so the switch must come first.
  3. Inside echo text, cmd metacharacters are escaped: no unescaped ( ) inside an
     if/for block, no bare & | < > in echo text, and no stray % (variables are
     %NAME% or %~dp0 only).

Limits: the scanner cannot tell a Path.open() from another object's open() when the
call has no arguments, so it does not flag argument-less .open(). It does not check
non-literal modes. It also cannot run cmd.exe, so the batch check is static only.
"""

import ast
import re
import textwrap
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".venv", "__pycache__", "build", "dist", "node_modules"}


# ------------------------------------------------------------------------------------------
# 1. Python text I/O must name its encoding
# ------------------------------------------------------------------------------------------

def _is_text_mode(mode: str) -> bool:
    return "b" not in mode


def encoding_problems(source: str, filename: str = "<sample>") -> list[str]:
    """Return one message per text-mode call that has no encoding argument."""
    tree = ast.parse(source, filename=filename)
    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        keyword_names = {k.arg for k in node.keywords}
        name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else None)
        flagged = None
        if name in ("read_text", "write_text") and "encoding" not in keyword_names:
            flagged = name
        elif name == "open":
            if isinstance(func, ast.Name):                       # builtin open(file, mode)
                mode_node = node.args[1] if len(node.args) > 1 else next(
                    (k.value for k in node.keywords if k.arg == "mode"), None)
                mode = "r" if mode_node is None else (
                    mode_node.value if isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str) else None)
                if mode is not None and _is_text_mode(mode) and "encoding" not in keyword_names:
                    flagged = f"open(mode={mode!r})"
            else:                                                # obj.open("mode") with a literal text mode
                mode_node = node.args[0] if node.args else next(
                    (k.value for k in node.keywords if k.arg == "mode"), None)
                if (isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str)
                        and _is_text_mode(mode_node.value) and "encoding" not in keyword_names):
                    flagged = f".open(mode={mode_node.value!r})"
        elif name in ("run", "Popen", "check_output", "call") and "encoding" not in keyword_names:
            if any(k.arg in ("text", "universal_newlines") and isinstance(k.value, ast.Constant) and k.value.value
                   for k in node.keywords):
                flagged = f"{name}(text=True)"
        if flagged:
            problems.append(f"{filename}:{node.lineno}: {flagged} has no encoding")
    return problems


def project_python_files(root: Path = PROJECT):
    for path in sorted(root.rglob("*.py")):
        if SKIP_DIRS & set(path.relative_to(root).parts):
            continue
        yield path


class PythonEncodingTests(unittest.TestCase):

    def test_every_text_io_call_in_the_project_names_an_encoding(self):
        problems = []
        for path in project_python_files():
            problems += encoding_problems(path.read_text(encoding="utf-8"), str(path.relative_to(PROJECT)))
        self.assertEqual(problems, [], "text-mode I/O without an encoding (breaks on Windows cp1252)")

    def test_the_scanner_flags_the_calls_it_is_meant_to_catch(self):
        bad = textwrap.dedent("""
            from pathlib import Path
            Path('a').read_text()
            Path('a').write_text('x')
            open('a')
            open('a', 'w')
            Path('a').open('a')
            import subprocess
            subprocess.run(['x'], text=True)
        """)
        self.assertEqual(len(encoding_problems(bad)), 6)

    def test_the_scanner_accepts_binary_calls_and_non_file_open_methods(self):
        good = textwrap.dedent("""
            from pathlib import Path
            Path('a').read_text(encoding='utf-8')
            Path('a').write_text('x', encoding='utf-8')
            open('a', 'rb')
            Path('a').open('wb')
            Image.open(io.BytesIO(data))
            opener.open(request, timeout=5)
            subprocess.run(['x'], capture_output=True, encoding='utf-8')
        """)
        self.assertEqual(encoding_problems(good), [])

    def test_the_install_bat_read_in_the_token_file_test_names_utf8(self):
        # Regression: this exact read raised UnicodeDecodeError on the Windows CI runner.
        source = (PROJECT / "tests" / "test_token_file.py").read_text(encoding="utf-8")
        line = next(l for l in source.splitlines() if "install.bat" in l and "read_text" in l)
        self.assertIn("encoding='utf-8'", line)


# ------------------------------------------------------------------------------------------
# 2 and 3. Batch files
# ------------------------------------------------------------------------------------------

def significant_chars(line: str):
    """(index, char) for cmd metacharacters outside double quotes and not caret-escaped."""
    out, quoted, i = [], False, 0
    while i < len(line):
        c = line[i]
        if c == '"':
            quoted = not quoted
        elif not quoted and c == "^":
            i += 2
            continue
        elif not quoted:
            out.append((i, c))
        i += 1
    return out


def batch_problems(raw: bytes, name: str = "<sample>") -> list[str]:
    problems = []
    if raw.startswith(b"\xef\xbb\xbf"):
        problems.append(f"{name}: has a UTF-8 BOM (cmd would show it as text on line 1)")
    if raw.count(b"\n") != raw.count(b"\r\n"):
        problems.append(f"{name}: contains bare LF line endings (cmd needs CRLF)")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return problems + [f"{name}: not valid UTF-8 ({exc.reason})"]

    lines = text.replace("\r\n", "\n").split("\n")
    chcp_at = next((n for n, l in enumerate(lines, 1) if re.match(r"\s*chcp\s+65001\b", l, re.I)), None)
    first_non_ascii = next((n for n, l in enumerate(lines, 1) if any(ord(ch) > 127 for ch in l)), None)
    if first_non_ascii is not None and (chcp_at is None or chcp_at > first_non_ascii):
        problems.append(f"{name}: non-ASCII text on line {first_non_ascii} comes before 'chcp 65001'")

    depth = 0
    for n, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.lower().startswith(("rem ", "::")) or stripped.startswith(":"):
            continue
        sig = significant_chars(line)
        match = re.search(r"(?i)(^|\s)echo(?=[\s.(]|$)", line)
        if match:
            start = match.start() + len(match.group(1))
            for index, char in sig:
                if index < start:
                    continue
                if char in "()" and depth > 0:
                    problems.append(f"{name}:{n}: unescaped '{char}' in echo text inside a block")
                elif char in "&|<>":
                    problems.append(f"{name}:{n}: unescaped '{char}' in echo text")
            cleaned = re.sub(r"%%|%~?[A-Za-z_][A-Za-z0-9_]*%|%~dp0", "", line[start:])
            if "%" in cleaned:
                problems.append(f"{name}:{n}: stray '%' in echo text")
        for _, char in sig:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
    if depth != 0:
        problems.append(f"{name}: unbalanced parentheses at end of file (depth {depth})")
    return problems


BATCH_FILES = sorted(PROJECT.glob("*.bat"))


class BatchFileTests(unittest.TestCase):

    def test_batch_files_exist(self):
        self.assertTrue(BATCH_FILES, "no .bat files found")

    def test_every_batch_file_is_utf8_crlf_and_switches_to_utf8_first(self):
        problems = []
        for path in BATCH_FILES:
            problems += batch_problems(path.read_bytes(), path.name)
        self.assertEqual(problems, [])

    def test_every_batch_echo_line_is_cmd_safe(self):
        problems = []
        for path in BATCH_FILES:
            problems += [p for p in batch_problems(path.read_bytes(), path.name)
                         if "echo" in p or "stray" in p or "unescaped" in p]
        self.assertEqual(problems, [])

    def test_the_batch_checker_catches_known_bad_lines(self):
        cases = {
            "paren inside a block": "chcp 65001 >nul\r\nif exist x (\r\n    echo Arabic (note) text\r\n)\r\n",
            "bare ampersand in echo": "chcp 65001 >nul\r\necho a & b\r\n",
            "bare percent in echo": "chcp 65001 >nul\r\necho 50% done\r\n",
            "Arabic before chcp": "echo بالعربية\r\nchcp 65001 >nul\r\n",
            "bare LF": "chcp 65001 >nul\necho ok\r\n",
            "BOM": b"\xef\xbb\xbf" + b"chcp 65001 >nul\r\n",
        }
        for label, body in cases.items():
            raw = body if isinstance(body, bytes) else body.encode("utf-8")
            self.assertTrue(batch_problems(raw, label), f"checker missed: {label}")

    def test_the_batch_checker_accepts_escaped_and_quoted_text(self):
        good = ("@echo off\r\nchcp 65001 >nul\r\nif exist x (\r\n"
                "    echo Use ^(this^) and \"a (b) & c\" here\r\n"
                "    echo بالعربية: شغّل install.bat أولًا.\r\n)\r\n"
                "echo done (outside the block)\r\n"
                "for /f \"delims=\" %%v in ('python -c \"print((1))\" 2^>nul') do set \"V=%%v\"\r\n")
        self.assertEqual(batch_problems(good.encode("utf-8"), "good"), [])


if __name__ == "__main__":
    unittest.main()
