"""Tests that need real Windows (Round 7). They skip everywhere else.

Run them on a Windows machine or the GitHub ``windows-latest`` runner, for example::

    python -m unittest discover -s tests -p "test_windows_real.py" -v

They use real files, real icacls, real sockets and real 8.3 short names. They never
touch the clipboard, the screen, the keyboard or the mouse. The only exception to the
Windows-only rule is ``IcaclsParserTests``, which checks the parser on canned text and
therefore runs everywhere.
"""

from __future__ import annotations

import asyncio
import csv
import http.client
import io
import locale
import logging
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from _bootstrap import PROJECT_DIR
assert PROJECT_DIR.is_dir()

from assistant import audit, token_store  # noqa: E402
from assistant.actions.base import (ActionError, STARTUP_FOLDER_MARKER,  # noqa: E402
                                    check_path_syntax, ensure_not_protected)
from assistant.config import normalize_path, path_is_inside  # noqa: E402
from assistant.remote.gate import AccessGate, generate_secret  # noqa: E402
from assistant.remote.server import COOKIE_NAME, RemoteServer  # noqa: E402
from assistant.remote.stream import FrameBroker  # noqa: E402

ONLY_WINDOWS = unittest.skipUnless(os.name == "nt", "needs real Windows")

TOKEN = "123456789:AAHwindowsRealTest_abcdefghijklmnopqrstuvwxyz"
ARABIC_FOLDER = "مجلد الاختبار"
SPACED_FOLDER = "tpa windows test"


# ---------------------------------------------------------------------------
# Helpers (plain functions, usable on every platform)
# ---------------------------------------------------------------------------

def parse_icacls_accounts(output: str, path: str) -> set[str]:
    """Account names that icacls lists for one file or folder.

    Each access entry has the form ``<account>:(<rights>)``. The first line starts
    with the path itself, so the path is removed before the entry is read. Lines
    without an entry (``Successfully processed 1 files`` and so on) are ignored.
    """
    accounts: set[str] = set()
    for raw in output.splitlines():
        line = raw.strip()
        if path and path in line:
            line = line.split(path, 1)[1].strip()
        if ":(" not in line:
            continue
        account = line.split(":(", 1)[0].strip()
        if account:
            accounts.add(account)
    return accounts


def _short_name(account: str) -> str:
    """``DOMAIN\\user`` -> ``user``, lower-cased, so display differences do not matter."""
    return account.rsplit("\\", 1)[-1].casefold()


def _run_text(args: list[str], cwd: Path | None = None) -> str:
    """Run a Windows command and decode its output the way the console shows it."""
    encoding = locale.getpreferredencoding(False) or "utf-8"
    proc = subprocess.run(args, capture_output=True, check=False, cwd=cwd)
    return proc.stdout.decode(encoding, errors="replace")


def current_identity() -> tuple[str, str]:
    """(account name, SID) of the signed-in user, from ``whoami /user``."""
    text = _run_text(["whoami", "/user", "/fo", "csv", "/nh"])
    row = next(csv.reader(io.StringIO(text.strip())))
    return row[0], row[1]


def icacls_accounts(path: Path) -> set[str]:
    """Accounts on one file or folder, read from icacls run in the parent folder.

    icacls prints its output in the console code page, which can garble non-ASCII
    names. Running it from the parent with only the ASCII leaf name keeps the path
    out of the output, so the parse is exact even when the folders contain Arabic.
    """
    leaf = path.name
    output = _run_text(["icacls", leaf], cwd=path.parent)
    return parse_icacls_accounts(output, leaf)


def _loopback_get(port: int, path: str, host: str, headers: dict | None = None):
    """One plain HTTP/1.1 GET over a real socket. Returns (status, headers, body)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host)
        for name, value in (headers or {}).items():
            conn.putheader(name, value)
        conn.endheaders()
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, body
    finally:
        conn.close()


def _short_path(path: str) -> str:
    """The 8.3 short form of a path, or the input unchanged if none exists."""
    import ctypes
    buf = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, len(buf))  # type: ignore[attr-defined]
    if length == 0:
        raise OSError(f"GetShortPathNameW failed for {path}")
    return buf.value


def _new_folder(prefix: str) -> Path:
    """A temp folder whose name has spaces and Arabic letters, removed at cleanup."""
    return Path(tempfile.mkdtemp(prefix=f"{prefix} {ARABIC_FOLDER} "))


# ---------------------------------------------------------------------------
# Parser checks: run everywhere
# ---------------------------------------------------------------------------

class IcaclsParserTests(unittest.TestCase):

    def test_single_owner_entry_after_the_path(self):
        path = r"C:\Users\me\مجلد الاختبار\token.json"
        output = f"{path} RUNNER\\runneradmin:(F)\n\nSuccessfully processed 1 files; Failed processing 0 files\n"
        self.assertEqual(parse_icacls_accounts(output, path), {"RUNNER\\runneradmin"})

    def test_every_entry_is_counted_including_inherited_ones(self):
        path = r"C:\x\token.json"
        output = (f"{path} NT AUTHORITY\\SYSTEM:(I)(F)\n"
                  "                BUILTIN\\Users:(I)(RX)\n"
                  "                RUNNER\\me:(F)\n")
        self.assertEqual(parse_icacls_accounts(output, path),
                         {"NT AUTHORITY\\SYSTEM", "BUILTIN\\Users", "RUNNER\\me"})

    def test_lines_without_entries_are_ignored(self):
        self.assertEqual(parse_icacls_accounts("Access is denied.\n", r"C:\x"), set())


# ---------------------------------------------------------------------------
# Token store on a real file
# ---------------------------------------------------------------------------

@ONLY_WINDOWS
class TokenStoreWindowsTests(unittest.TestCase):

    def setUp(self) -> None:
        self.root = _new_folder("tpa token")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_current_user_sid_is_a_valid_sid_for_this_user(self):
        sid = token_store._current_user_sid()
        self.assertIsNotNone(sid, "PowerShell did not return a SID")
        self.assertRegex(sid, r"^S-1-\d+(-\d+)+$")
        _account, whoami_sid = current_identity()
        self.assertEqual(sid, whoami_sid)

    def test_lock_down_leaves_only_the_current_user_on_a_real_file(self):
        target = self.root / "token.json"
        target.write_text("{}", encoding="utf-8")
        method = token_store._lock_down(target)
        self.assertEqual(method, "icacls")
        account, _sid = current_identity()
        accounts = icacls_accounts(target)
        self.assertEqual({_short_name(a) for a in accounts}, {_short_name(account)},
                         f"icacls lists {accounts}; only the current user may have access")

    def test_save_then_read_back_in_a_folder_with_spaces_and_arabic_letters(self):
        target = self.root / ARABIC_FOLDER / "store with spaces" / "token.json"
        saved = token_store.save_token(TOKEN, path=target)
        self.assertEqual(saved, target)
        self.assertEqual(token_store.load_stored_token(target), TOKEN)

        # DPAPI is the normal protection on Windows: the token must not be readable on disk.
        self.assertEqual(token_store.protection_of(target), "dpapi")
        self.assertNotIn(TOKEN, target.read_text(encoding="utf-8"))

        account, _sid = current_identity()
        for path in (target, target.parent):
            accounts = icacls_accounts(path)
            self.assertEqual({_short_name(a) for a in accounts}, {_short_name(account)},
                             f"icacls on {path.name} lists {accounts}")

        self.assertTrue(token_store.delete_token(target))
        self.assertFalse(target.exists())


# ---------------------------------------------------------------------------
# Remote server on real sockets
# ---------------------------------------------------------------------------

@ONLY_WINDOWS
class RemoteServerWindowsTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        self.secret = generate_secret()
        self.gate = AccessGate(self.secret, deadline=time.monotonic() + 600)
        self.server = RemoteServer(
            gate=self.gate, broker=FrameBroker(),
            on_input=lambda event: None, on_viewer=lambda connected: None,
            on_sent=lambda seconds: None, stats=lambda: {},
        )
        self.port = await self.server.start()
        self.addAsyncCleanup(self.server.close)
        self.host = f"127.0.0.1:{self.port}"

    async def get(self, path: str, host: str | None = None, headers: dict | None = None):
        return await asyncio.to_thread(_loopback_get, self.port, path, host or self.host, headers)

    async def test_listens_on_loopback_only(self):
        address = self.server._server.sockets[0].getsockname()
        self.assertEqual(address[0], "127.0.0.1")

    async def test_request_without_a_cookie_gets_403(self):
        status, _headers, _body = await self.get("/")
        self.assertEqual(status, 403)

    async def test_unknown_path_without_a_cookie_is_403_not_404(self):
        status, _headers, _body = await self.get("/no-such-page")
        self.assertEqual(status, 403)

    async def test_wrong_secret_gets_403(self):
        status, _headers, _body = await self.get("/connect/" + "x" * 43)
        self.assertEqual(status, 403)

    async def test_foreign_host_is_refused_and_does_not_use_up_the_secret(self):
        status, _headers, _body = await self.get(f"/connect/{self.secret}", host="evil.example")
        self.assertEqual(status, 403)
        status, headers, _body = await self.get(f"/connect/{self.secret}")
        self.assertEqual(status, 303, "a refused Host must not consume the one-time link")

    async def test_correct_secret_sets_a_cookie_and_the_page_then_returns_200(self):
        status, headers, _body = await self.get(f"/connect/{self.secret}")
        self.assertEqual(status, 303)
        set_cookie = headers.get("set-cookie", "")
        self.assertTrue(set_cookie.startswith(f"{COOKIE_NAME}="), set_cookie)
        cookie = set_cookie.split(";", 1)[0].split("=", 1)[1]

        status, headers, body = await self.get("/", headers={"Cookie": f"{COOKIE_NAME}={cookie}"})
        self.assertEqual(status, 200)
        self.assertTrue(headers.get("content-type", "").startswith("text/html"))
        self.assertIn(b"<", body)

    async def test_the_link_cannot_be_redeemed_twice(self):
        status, _headers, _body = await self.get(f"/connect/{self.secret}")
        self.assertEqual(status, 303)
        status, _headers, _body = await self.get(f"/connect/{self.secret}")
        self.assertEqual(status, 403)


# ---------------------------------------------------------------------------
# Path guard on real Windows paths
# ---------------------------------------------------------------------------

@ONLY_WINDOWS
class WindowsPathGuardTests(unittest.TestCase):

    def test_drive_root_is_an_ordinary_local_path(self):
        check_path_syntax("C:\\")
        ensure_not_protected(Path("C:\\"))

    def test_unc_and_device_spellings_are_refused(self):
        for raw in (r"\\server\share\x.txt", "//server/share/x.txt", r"\\?\C:\x.txt",
                    r"\\.\pipe\x", r"\??\UNC\server\share\x.txt"):
            with self.subTest(raw=raw), self.assertRaises(ActionError):
                check_path_syntax(raw)

    def test_alternate_data_stream_is_refused(self):
        with self.assertRaises(ActionError):
            check_path_syntax(r"C:\Users\me\notes.txt:hidden")

    def test_reserved_device_names_are_refused_in_any_folder(self):
        for raw in (r"C:\temp\CON.txt", r"C:\temp\NUL", r"C:\temp\COM1.log", r"C:\temp\CONIN$"):
            with self.subTest(raw=raw), self.assertRaises(ActionError):
                check_path_syntax(raw)

    def test_trailing_dot_or_space_does_not_hide_the_token_file(self):
        for raw in (r"C:\Users\me\token.txt.", r"C:\Users\me\token.txt "):
            with self.subTest(raw=raw), self.assertRaises(ActionError):
                ensure_not_protected(Path(raw))

    def test_8_3_name_of_the_token_file_is_protected(self):
        with self.assertRaises(ActionError):
            ensure_not_protected(Path(r"C:\Users\me\TOKEN~1.TXT"))

    def test_arabic_folders_are_allowed_and_a_token_file_in_one_is_not(self):
        ok = Path(r"C:\Users\me") / ARABIC_FOLDER / "ملف.txt"
        check_path_syntax(str(ok))
        ensure_not_protected(ok)
        with self.assertRaises(ActionError):
            ensure_not_protected(Path(r"C:\Users\me") / ARABIC_FOLDER / "token.txt")

    def test_writes_into_the_startup_folder_are_refused(self):
        appdata = os.environ.get("APPDATA")
        if not appdata:
            self.skipTest("APPDATA is not set")
        startup = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        self.assertIn(STARTUP_FOLDER_MARKER, str(startup).lower().replace("/", "\\"))
        with self.assertRaises(ActionError):
            ensure_not_protected(startup / "x.lnk", for_write=True)

    def test_real_8_3_short_name_of_a_folder_resolves_to_the_same_folder(self):
        root = _new_folder("tpa short")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        folder = root / "long folder name"
        folder.mkdir()
        short = _short_path(str(folder))
        if short == str(folder):
            self.skipTest("8.3 names are disabled on this volume")
        self.assertTrue(path_is_inside(normalize_path(Path(short)), normalize_path(root)))
        self.assertEqual(normalize_path(Path(short)), normalize_path(folder))

    def test_real_8_3_short_name_of_a_token_file_is_protected(self):
        root = _new_folder("tpa short token")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        token_file = root / "token.txt"
        token_file.write_text("not a real token", encoding="utf-8")
        short = _short_path(str(token_file))
        if short == str(token_file):
            self.skipTest("8.3 names are disabled on this volume")
        with self.assertRaises(ActionError):
            ensure_not_protected(Path(short))
        with self.assertRaises(ActionError):
            ensure_not_protected(normalize_path(Path(short)))


# ---------------------------------------------------------------------------
# Log rotation: handles are released
# ---------------------------------------------------------------------------

@ONLY_WINDOWS
class LogRotationWindowsTests(unittest.TestCase):

    def setUp(self) -> None:
        audit.close()  # start from a clean logger, even if another test configured it
        self.root = _new_folder("tpa log")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(audit.close)

    def test_rotated_logs_can_be_deleted_after_the_handlers_are_closed(self):
        folder = self.root / "logs"
        log = folder / "assistant.log"
        audit.setup(log, "ERROR")
        logger = logging.getLogger("assistant")
        line = "x" * 1000
        for _ in range(2300):  # about 2.3 MB, past the 2,000,000-byte rotation limit
            logger.info("%s", line)
        self.assertTrue((folder / "assistant.log.1").exists(), "the log should have rotated")

        audit.close()
        self.assertEqual(logging.getLogger("assistant").handlers, [])
        # Windows refuses to delete a file that a handle still has open (WinError 32).
        for path in list(folder.iterdir()):
            path.unlink()
        self.assertEqual(list(folder.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
