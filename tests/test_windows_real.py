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
import base64
import csv
import http.client
import io
import json
import locale
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from _bootstrap import PROJECT_DIR
assert PROJECT_DIR.is_dir()

from assistant import audit, token_store  # noqa: E402
from assistant.actions.base import (ActionError, STARTUP_FOLDER_MARKER,  # noqa: E402
                                    check_path_syntax, ensure_not_protected)
from assistant.config import normalize_path, path_is_inside  # noqa: E402
from assistant.actions import base as action_base  # noqa: E402
from assistant.actions import core as core_mod  # noqa: E402
from assistant.actions import system as system_mod  # noqa: E402
from assistant.actions import transfer as transfer_mod  # noqa: E402
from assistant.actions.base import ActionContext  # noqa: E402
from test_flow import make_config  # noqa: E402
from test_round11_quality import (GRANDCHILD_SRC, PARENT_SRC, assert_stopped,  # noqa: E402
                                  wait_for)
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

def _why() -> str:
    """The sanitized reason from the last PowerShell call (Round 9), for failure messages."""
    reason = token_store.last_powershell_error()
    return f" [PowerShell reason: {reason}]" if reason else " [no PowerShell reason recorded]"


@ONLY_WINDOWS
class TokenStoreWindowsTests(unittest.TestCase):

    def setUp(self) -> None:
        self.root = _new_folder("tpa token")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_current_user_sid_is_a_valid_sid_for_this_user(self):
        sid = token_store._current_user_sid()
        self.assertIsNotNone(sid, "PowerShell did not return a SID." + _why())
        self.assertRegex(sid, r"^S-1-\d+(-\d+)+$")
        _account, whoami_sid = current_identity()
        self.assertEqual(sid, whoami_sid)

    def test_lock_down_leaves_only_the_current_user_on_a_real_file(self):
        target = self.root / "token.json"
        target.write_text("{}", encoding="utf-8")
        method = token_store._lock_down(target)
        self.assertEqual(method, "acl", f"lock-down used {method!r}." + _why())
        account, sid = current_identity()
        # Independent check: icacls lists exactly the current user, and no SYSTEM,
        # Administrators or OWNER RIGHTS entry is left behind.
        accounts = icacls_accounts(target)
        self.assertEqual({_short_name(a) for a in accounts}, {_short_name(account)},
                         f"icacls lists {accounts}; only the current user may have access")
        # Same answer from the SID-level read-back: one explicit entry, and the user owns the file.
        self.assertTrue(token_store.acl_is_owner_only(token_store.windows_acl_report(target), sid))

    def test_save_then_read_back_in_a_folder_with_spaces_and_arabic_letters(self):
        target = self.root / ARABIC_FOLDER / "store with spaces" / "token.json"
        try:
            saved = token_store.save_token(TOKEN, path=target)
        except RuntimeError as exc:
            # The save fails closed; show the reason (never the token) in the failure text.
            self.fail(f"save_token refused: {exc}" + _why())
        self.assertEqual(saved, target)
        self.assertEqual(token_store.load_stored_token(target), TOKEN)

        # DPAPI is the normal protection on Windows: the token must not be readable on disk.
        stored = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(stored["protection"], "dpapi")
        self.assertNotIn(TOKEN, target.read_text(encoding="utf-8"))
        # The label is a human sentence, so check its start rather than the stored word.
        self.assertTrue(token_store.protection_of(target).startswith("DPAPI"),
                        token_store.protection_of(target))

        account, sid = current_identity()
        for path in (target, target.parent):
            accounts = icacls_accounts(path)
            self.assertEqual({_short_name(a) for a in accounts}, {_short_name(account)},
                             f"icacls on {path.name} lists {accounts}")
            self.assertTrue(token_store.acl_is_owner_only(token_store.windows_acl_report(path), sid),
                            f"read-back of {path.name} is not owner-only")

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


# ---------------------------------------------------------------------------
# Round 8: links, the Administrator check, and folder hardening on real Windows
# ---------------------------------------------------------------------------

def _make_junction(link: Path, target: Path) -> None:
    """A directory junction, made by PowerShell. Needs no special privilege, unlike a symbolic link.

    The paths travel in environment variables, so spaces and Arabic letters survive.
    """
    script = ("New-Item -ItemType Junction -Path $env:TPA_JUNCTION_LINK "
              "-Target $env:TPA_JUNCTION_TARGET | Out-Null")
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    env = dict(os.environ, TPA_JUNCTION_LINK=str(link), TPA_JUNCTION_TARGET=str(target))
    proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                          capture_output=True, check=False, env=env)
    if proc.returncode != 0 or not link.exists():
        raise unittest.SkipTest(f"could not create a junction: {proc.stderr[-300:]!r}")


@ONLY_WINDOWS
class WindowsLinkAndElevationTests(unittest.TestCase):

    def setUp(self) -> None:
        self.root = _new_folder("tpa link")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.workspace = self.root / ARABIC_FOLDER / "workspace"
        self.outside = self.root / "outside folder"
        self.workspace.mkdir(parents=True)
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("outside", encoding="utf-8")

    def _config(self):
        from types import SimpleNamespace
        return SimpleNamespace(allowed_roots=[self.workspace], workspace_root=self.workspace)

    def test_junction_pointing_outside_the_roots_is_refused(self):
        from assistant.actions.base import resolve_in_roots
        junction = self.workspace / "linked folder"
        _make_junction(junction, self.outside)
        with self.assertRaises(ActionError):
            resolve_in_roots(str(junction / "secret.txt"), self._config(), "File path")
        with self.assertRaises(ActionError):
            resolve_in_roots("linked folder\\secret.txt", self._config(), "File path")

    def test_file_symlink_pointing_outside_the_roots_is_refused(self):
        from assistant.actions.base import resolve_in_roots
        link = self.workspace / "link.txt"
        try:
            os.symlink(self.outside / "secret.txt", link)
        except OSError as exc:
            self.skipTest(f"symbolic links need a privilege this account lacks: {exc}")
        with self.assertRaises(ActionError):
            resolve_in_roots(str(link), self._config(), "File path")

    def test_is_administrator_gives_a_definite_answer_on_windows(self):
        from assistant import elevation
        answer = elevation.is_administrator()
        self.assertIn(answer, (True, False), "IsUserAnAdmin should always answer on Windows")

    def test_harden_dir_on_a_real_folder_leaves_only_the_current_user(self):
        folder = self.root / ARABIC_FOLDER / "owner only folder"
        folder.mkdir(parents=True)
        self.assertTrue(token_store._harden_dir(folder))
        account, sid = current_identity()
        accounts = icacls_accounts(folder)
        self.assertEqual({_short_name(a) for a in accounts}, {_short_name(account)})
        self.assertTrue(token_store.acl_is_owner_only(token_store.windows_acl_report(folder), sid))



@ONLY_WINDOWS
class WingetAndHealthWindowsTests(unittest.TestCase):
    """Round 9, read-only only: winget --version and the /health probes. Nothing is installed."""

    def test_winget_version_runs_and_exits_zero(self):
        exe = shutil.which("winget")
        if exe is None:
            self.skipTest("winget is not installed on this runner")
        proc = subprocess.run([exe, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=120, stdin=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 0,
                         msg=f"winget --version exited {proc.returncode}: {(proc.stderr or proc.stdout)[:200]!r}")
        self.assertRegex(proc.stdout + proc.stderr, r"\d+\.\d+")

    def test_health_probes_return_a_full_report_without_crashing(self):
        from assistant import health
        checks = health.run_health()
        self.assertGreaterEqual(len(checks), 8)
        self.assertTrue(all(c.status in {health.OK, health.WARN, health.UNKNOWN} for c in checks))
        user = os.environ.get("USERNAME", "")
        text = health.render(checks, "en")
        if user:
            self.assertNotIn(user, text)
        self.assertNotIn("\\Users\\", text)


# ---------------------------------------------------------------------------
# Round 11: NTFS file edge cases, escape attempts, subprocess trees, logs.
# Never touches the clipboard, screen, keyboard or mouse (Round 7 rule).
# ---------------------------------------------------------------------------

def _hold_exclusive(path: Path):
    """Open ``path`` with no sharing, the way a program that locks a file does.

    Returns ``(handle, release)``. Call ``release()`` before the folder is removed.
    """
    import ctypes
    from ctypes import wintypes
    generic_read, open_existing, normal = 0x80000000, 3, 0x80
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.HANDLE]
    handle = kernel32.CreateFileW(str(path), generic_read, 0, None, open_existing, normal, None)
    invalid = ctypes.c_void_p(-1).value
    if not handle or handle == invalid:
        raise OSError(ctypes.get_last_error(), f"could not lock {path.name}")

    def release() -> None:
        kernel32.CloseHandle(handle)

    return handle, release


class _FakeTransport:
    """Stands in for Telegram: writes a fixed payload where the bot would download it."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def download_file(self, file_id: str, destination: Path) -> int:
        destination.write_bytes(self.payload)
        return len(self.payload)


class _NtfsFixture(unittest.TestCase):
    """A real NTFS workspace with a secret folder outside it. Holds no tests."""

    SECRET = "TOP SECRET CONTENT"

    def setUp(self) -> None:
        self.root = _new_folder("tpa ntfs")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.ws = self.root / ARABIC_FOLDER / "workspace"
        self.ws.mkdir(parents=True)
        self.outside = self.root / "outside folder"
        self.outside.mkdir()
        self.secret = self.outside / "secret.txt"
        self.secret.write_text(self.SECRET, encoding="utf-8")
        (self.ws / "notes.txt").write_text("hello notes", encoding="utf-8")
        self.config = make_config(self.root / "cfg")
        self.config.allowed_roots = [self.ws]
        self.config.workspace_root = self.ws
        self.config.downloads_dir = self.ws / "downloads"
        self.ctx = ActionContext(config=self.config)

    def _assert_refused_or_inside(self, raw: str) -> None:
        try:
            result = core_mod.read_file({"path": raw}, self.ctx)
        except ActionError:
            return
        first_line = result.output.splitlines()[0]
        self.assertTrue(path_is_inside(Path(first_line), self.ws),
                        f"{raw!r} read {first_line!r}, outside the allowed folder")
        self.assertNotIn(self.SECRET, result.output)

@ONLY_WINDOWS
class WindowsNtfsFileTests(_NtfsFixture):
    """read_file, send_file and receive_file on a real NTFS folder."""

    def test_spaces_and_arabic_names_are_read(self):
        folder = self.ws / "مجلد فرعي بمسافات"
        folder.mkdir()
        name = folder / "ملف عربي 1.txt"
        name.write_text("نص عربي", encoding="utf-8")
        result = core_mod.read_file({"path": str(name)}, self.ctx)
        self.assertIn("نص عربي", result.output)

    def test_a_path_over_260_characters_is_read_or_refused_cleanly(self):
        deep = self.ws
        for index in range(12):
            deep = deep / f"segment long name {index:02d} for the test"
        os.makedirs("\\\\?\\" + str(deep))  # extended-length prefix, so the folders can be made
        (deep / "far.txt").write_text("far away", encoding="utf-8")
        self.assertGreater(len(str(deep / "far.txt")), 260)
        try:
            result = core_mod.read_file({"path": str(deep / "far.txt")}, self.ctx)
        except ActionError:
            return  # refused: acceptable when long-path support is off
        self.assertIn("far away", result.output)

    def test_device_names_are_refused_before_anything_is_opened(self):
        for name in ("CON", "nul", "COM1", "LPT1.txt", "aux.log"):
            with self.subTest(name=name):
                with self.assertRaises(ActionError):
                    check_path_syntax(str(self.ws / name), "File path")
                with self.assertRaises(ActionError):
                    core_mod.read_file({"path": str(self.ws / name)}, self.ctx)

    def test_trailing_dot_or_space_resolves_inside_or_is_refused(self):
        (self.ws / "real.txt").write_text("REAL", encoding="utf-8")
        for raw in (str(self.ws / "real.txt."), str(self.ws / "real.txt ")):
            with self.subTest(raw=raw):
                self._assert_refused_or_inside(raw)

    def test_alternate_data_stream_is_refused(self):
        stream = str(self.ws / "notes.txt") + ":hidden"
        (self.ws / "notes.txt:hidden").write_text("stream text", encoding="utf-8")
        with self.assertRaises(ActionError):
            core_mod.read_file({"path": stream}, self.ctx)

    def test_unc_path_is_refused_without_a_network_lookup(self):
        with self.assertRaises(ActionError) as caught:
            core_mod.read_file({"path": r"\\server\share\x.txt"}, self.ctx)
        self.assertIn("UNC", str(caught.exception))

    def test_drive_relative_path_is_refused_or_inside(self):
        self._assert_refused_or_inside("C:notes.txt")

    def test_an_8_3_short_name_inside_the_roots_is_read(self):
        (self.ws / "short name file.txt").write_text("short ok", encoding="utf-8")
        short = _short_path(str(self.ws / "short name file.txt"))
        if short == str(self.ws / "short name file.txt"):
            self.skipTest("this volume has no 8.3 short names")
        result = core_mod.read_file({"path": short}, self.ctx)
        self.assertIn("short ok", result.output)

    def test_case_differences_match_the_allowed_folder(self):
        self.config.allowed_roots = [Path(str(self.ws).upper())]
        result = core_mod.read_file({"path": str(self.ws).lower() + "\\notes.txt"}, self.ctx)
        self.assertIn("hello notes", result.output)

    def test_forward_and_back_slashes_both_work(self):
        for raw in (str(self.ws / "notes.txt"), str(self.ws / "notes.txt").replace("\\", "/")):
            with self.subTest(raw=raw):
                self.assertIn("hello notes", core_mod.read_file({"path": raw}, self.ctx).output)

    def test_a_read_only_file_can_still_be_read_and_sent(self):
        target = self.ws / "readonly.txt"
        target.write_text("read only text", encoding="utf-8")
        os.chmod(target, stat.S_IREAD)
        self.addCleanup(os.chmod, target, stat.S_IREAD | stat.S_IWRITE)
        self.assertIn("read only text", core_mod.read_file({"path": str(target)}, self.ctx).output)
        self.assertTrue(transfer_mod.send_file({"path": str(target)}, self.ctx).ok)

    def test_a_file_locked_by_another_process_gives_a_short_reason(self):
        target = self.ws / "locked.txt"
        target.write_text("locked", encoding="utf-8")
        _handle, release = _hold_exclusive(target)
        self.addCleanup(release)  # registered after the folder cleanup, so it runs first
        with self.assertRaises(ActionError) as caught:
            core_mod.read_file({"path": str(target)}, self.ctx)
        message = str(caught.exception)
        self.assertIn("access was denied", message)
        self.assertNotIn("Errno", message)

    def test_junction_escape_is_refused_for_read_send_and_receive(self):
        junction = self.ws / "linked folder"
        _make_junction(junction, self.outside)
        with self.assertRaises(ActionError):
            core_mod.read_file({"path": str(junction / "secret.txt")}, self.ctx)
        with self.assertRaises(ActionError):
            transfer_mod.send_file({"path": str(junction / "secret.txt")}, self.ctx)
        receive_ctx = ActionContext(config=self.config, transport=_FakeTransport(b"payload"))
        with self.assertRaises(ActionError):
            transfer_mod.receive_file({"file_id": "abc", "size": "7",
                                       "target": "linked folder\\new.txt"}, receive_ctx)
        self.assertFalse((self.outside / "new.txt").exists())


@ONLY_WINDOWS
class WindowsEscapeAttemptTests(_NtfsFixture):
    """Escape spellings that only mean something on Windows."""

    ESCAPES = (
        "..\\outside folder\\secret.txt",
        "notes.txt\\..\\..\\outside folder\\secret.txt",
        "..\\..\\outside folder/secret.txt",
        "\\\\?\\" + "C:\\x\\secret.txt",
        "\\\\.\\" + "C:\\x\\secret.txt",
        "C:\\Windows\\win.ini",
    )

    def test_windows_escape_spellings_are_refused_or_inside(self):
        for raw in self.ESCAPES:
            with self.subTest(raw=raw):
                self._assert_refused_or_inside(raw)

    def test_environment_variables_are_expanded_then_checked(self):
        os.environ["TPA_R11_OUTSIDE"] = str(self.outside)
        self.addCleanup(os.environ.pop, "TPA_R11_OUTSIDE", None)
        self._assert_refused_or_inside("%TPA_R11_OUTSIDE%\\secret.txt")
        self._assert_refused_or_inside("%TPA_R11_OUTSIDE%/secret.txt")

    def test_home_shorthand_is_expanded_then_checked(self):
        self._assert_refused_or_inside("~\\secret.txt")
        self._assert_refused_or_inside("~\\..\\outside folder\\secret.txt")

    def test_trailing_dots_and_spaces_on_the_escape_are_refused_or_inside(self):
        for raw in ("..\\outside folder\\secret.txt.", "..\\outside folder\\secret.txt "):
            with self.subTest(raw=raw):
                self._assert_refused_or_inside(raw)


@ONLY_WINDOWS
class WindowsProcessTests(unittest.TestCase):
    """Real child processes. Harmless: a Python sleep and cmd echo only."""

    def setUp(self) -> None:
        action_base.clear_abort()
        self.addCleanup(action_base.clear_abort)
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.heartbeat = self.dir / "heartbeat.txt"
        (self.dir / "parent.py").write_text(PARENT_SRC, encoding="utf-8")
        (self.dir / "grand.py").write_text(GRANDCHILD_SRC, encoding="utf-8")

    def test_timeout_kills_a_grandchild_started_through_cmd(self):
        argv = ["cmd", "/c", sys.executable, str(self.dir / "parent.py"),
                str(self.heartbeat), str(self.dir / "grand.py")]
        outcome: dict[str, object] = {}

        def run() -> None:
            try:
                action_base.run_process(argv, timeout=3)
            except ActionError as exc:
                outcome["error"] = exc

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        self.assertTrue(wait_for(lambda: self.heartbeat.exists() and self.heartbeat.stat().st_size > 0, 60))
        worker.join(60)
        self.assertFalse(worker.is_alive(), "cmd /c tree kept the pipe open after the timeout")
        self.assertIn("timed out", str(outcome.get("error")))
        assert_stopped(self, self.heartbeat)

    def test_a_harmless_child_is_listed_and_killed_by_pid(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.addCleanup(lambda: child.poll() is None and child.kill())
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        ctx = ActionContext(config=make_config(Path(tmp.name)))
        listed = system_mod.list_processes({"sort": "name", "limit": "100"}, ctx)
        self.assertTrue(listed.ok)
        result = system_mod.kill_process({"pid": str(child.pid)}, ctx)
        self.assertTrue(result.ok)
        self.assertIsNotNone(child.wait(timeout=30))

    def test_code_page_output_from_cmd_decodes_without_error(self):
        proc = action_base.run_process(["cmd", "/c", "chcp 866 >nul && echo ok"], timeout=60)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("ok", proc.stdout)


@ONLY_WINDOWS
class WindowsLogFileTests(unittest.TestCase):

    def setUp(self) -> None:
        audit.close()
        self.addCleanup(audit.close)
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.log = self.dir / "logs" / "a.log"

    def test_a_log_file_locked_by_another_program_does_not_stop_startup(self):
        self.log.parent.mkdir(parents=True)
        self.log.write_text("locked", encoding="utf-8")
        _handle, release = _hold_exclusive(self.log)
        self.addCleanup(release)
        import io
        from contextlib import redirect_stderr
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            audit.setup(self.log, "INFO")
            audit.warning("still running")
        self.assertIn("console only", buffer.getvalue())

    def test_rotation_works_on_a_real_file(self):
        audit.setup(self.log, "CRITICAL")
        for _ in range(25000):
            audit.event("rotation", reason="rotation check")
        audit.close()
        self.assertTrue(self.log.with_name(self.log.name + ".1").exists())
        self.assertLessEqual(self.log.stat().st_size, 2_000_000)


if __name__ == "__main__":
    unittest.main()
