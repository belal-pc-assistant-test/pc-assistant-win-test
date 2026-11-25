"""Public HTTPS address for the local viewer.

Order (configurable): a Cloudflare *quick tunnel* (no account needed) first, then an
SSH reverse tunnel to localhost.run as a fallback. Both only forward traffic to the
loopback server; neither gets any access secret other than what the link carries.

``cloudflared.exe`` is downloaded on demand, only over HTTPS from the official
``cloudflare/cloudflared`` GitHub release, into the app's ``tools`` folder. Every
redirect is checked against an allowlist, the file is run with ``--version`` before
it is used, and an optional SHA-256 pin from config.ini is enforced.

The tunnel process is killed on stop. On Windows it is also placed in a job object
with "kill on close", so it cannot outlive the bot even if the bot crashes.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

PROVIDER_CLOUDFLARE = "cloudflare"
PROVIDER_LOCALHOST_RUN = "localhost.run"
PROVIDERS = (PROVIDER_CLOUDFLARE, PROVIDER_LOCALHOST_RUN)

CLOUDFLARED_WINDOWS_URL = ("https://github.com/cloudflare/cloudflared/releases/latest/"
                           "download/cloudflared-windows-amd64.exe")
DOWNLOAD_HOSTS = frozenset({"github.com", "objects.githubusercontent.com",
                            "release-assets.githubusercontent.com"})
DOWNLOAD_PATH_PREFIX = "/cloudflare/cloudflared/releases/"
MAX_DOWNLOAD_BYTES = 120 * 1024 * 1024
CREATE_NO_WINDOW = 0x08000000

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_CLOUDFLARE_RE = re.compile(rf"https://{_LABEL}\.trycloudflare\.com(?![A-Za-z0-9.-])")
_LOCALHOST_RUN_RE = re.compile(rf"https://{_LABEL}\.(?:lhr\.life|lhr\.rocks|localhost\.run)(?![A-Za-z0-9.-])")
_PATTERNS = {PROVIDER_CLOUDFLARE: _CLOUDFLARE_RE, PROVIDER_LOCALHOST_RUN: _LOCALHOST_RUN_RE}


class TunnelError(RuntimeError):
    """Tunnel or helper-binary problem. The message is safe to show the owner."""


def public_url_from_line(provider: str, line: str) -> str | None:
    """Pull the public https address out of one line of tunnel program output."""
    pattern = _PATTERNS.get(provider)
    if pattern is None:
        return None
    match = pattern.search(line)
    return match.group(0) if match else None


def tunnel_argv(provider: str, port: int, *, cloudflared: Path | None,
                ssh: str | None) -> list[str] | None:
    """Command line for a provider, or None when that provider is not available here."""
    if provider == PROVIDER_CLOUDFLARE:
        if cloudflared is None:
            return None
        return [str(cloudflared), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"]
    if provider == PROVIDER_LOCALHOST_RUN:
        if not ssh:
            return None
        return [ssh, "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
                "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=20",
                "-R", f"80:127.0.0.1:{port}", "nokey@localhost.run"]
    return None


# ---------------------------------------------------------------------------
# cloudflared: allowlisted download, checksum pin, verify before use
# ---------------------------------------------------------------------------

def validate_download_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme != "https" or parts.hostname != "github.com" or parts.username or
            parts.password or parts.port not in (None, 443) or
            not parts.path.startswith(DOWNLOAD_PATH_PREFIX)):
        raise TunnelError("Refusing to download from an unexpected address.")


def _redirect_allowed(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    return (parts.scheme == "https" and parts.hostname in DOWNLOAD_HOSTS and
            not parts.username and not parts.password)


class _AllowlistedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _redirect_allowed(newurl):
            raise TunnelError("A download redirect left the allowed hosts.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_cloudflared(destination_dir: Path, url: str = CLOUDFLARED_WINDOWS_URL,
                         timeout: float = 120) -> tuple[Path, str]:
    """Download to a ``.part`` file. Returns (path, sha256 hex). Nothing is run yet."""
    validate_download_url(url)
    destination_dir.mkdir(parents=True, exist_ok=True)
    partial = destination_dir / "cloudflared.exe.part"
    digest = hashlib.sha256()
    total = 0
    opener = urllib.request.build_opener(_AllowlistedRedirects())
    request = urllib.request.Request(url, headers={"User-Agent": "telegram-pc-assistant"})
    try:
        with opener.open(request, timeout=timeout) as response, partial.open("wb") as out:
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise TunnelError("The download is larger than expected; aborted.")
                digest.update(chunk)
                out.write(chunk)
    except TunnelError:
        partial.unlink(missing_ok=True)
        raise
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise TunnelError("Could not download cloudflared (no internet, or GitHub is unreachable).") from exc
    if total < 1024 * 1024:
        partial.unlink(missing_ok=True)
        raise TunnelError("The cloudflared download is incomplete.")
    return partial, digest.hexdigest()


def verify_binary(path: Path, timeout: float = 30) -> str:
    """Run ``<binary> --version`` and return its first line. Raises TunnelError otherwise."""
    try:
        completed = subprocess.run(
            [str(path), "--version"], capture_output=True, timeout=timeout,
            stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TunnelError("The tunnel program did not run.") from exc
    text = completed.stdout.decode("utf-8", "replace").strip()
    if completed.returncode != 0 or "cloudflared" not in text.lower():
        raise TunnelError("The tunnel program did not identify itself as cloudflared.")
    return text.splitlines()[0][:120]


def ensure_cloudflared(tools_dir: Path, *, pinned_sha256: str = "",
                       downloader: Callable[[Path], tuple[Path, str]] | None = None,
                       verifier: Callable[[Path], str] = verify_binary) -> Path:
    """Return a verified ``cloudflared.exe`` in ``tools_dir``, downloading it when missing."""
    exe = tools_dir / "cloudflared.exe"
    if exe.is_file():
        try:
            verifier(exe)
            return exe
        except TunnelError:
            exe.unlink(missing_ok=True)
    partial, sha = (downloader or download_cloudflared)(tools_dir)
    try:
        if pinned_sha256 and sha != pinned_sha256.lower():
            raise TunnelError("cloudflared failed its SHA-256 check; it was not installed.")
        verifier(partial)
        os.replace(partial, exe)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return exe


# ---------------------------------------------------------------------------
# Running a tunnel process
# ---------------------------------------------------------------------------

class _KillOnCloseJob:
    """Windows job object: when the bot exits, the tunnel process is terminated too."""

    def __init__(self, pid: int) -> None:
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.OpenProcess.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise OSError("CreateJobObjectW failed")
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(job)
            raise OSError("SetInformationJobObject failed")
        process = kernel32.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
        if not process:
            kernel32.CloseHandle(job)
            raise OSError("OpenProcess failed")
        assigned = kernel32.AssignProcessToJobObject(job, process)
        kernel32.CloseHandle(process)
        if not assigned:
            kernel32.CloseHandle(job)
            raise OSError("AssignProcessToJobObject failed")
        self._job = job
        self._kernel32 = kernel32

    def close(self) -> None:
        if self._job:
            self._kernel32.CloseHandle(self._job)
            self._job = None


class TunnelProcess:
    """A running tunnel program. Output is read in a thread and only scanned for the address."""

    def __init__(self, provider: str, process: subprocess.Popen, loop: asyncio.AbstractEventLoop) -> None:
        self.provider = provider
        self._process = process
        self._loop = loop
        self._queue: asyncio.Queue = asyncio.Queue()
        self._scanning = True
        self._job: _KillOnCloseJob | None = None
        self._reader = threading.Thread(target=self._pump, name="tunnel-output", daemon=True)
        self._reader.start()
        if sys.platform == "win32":
            try:
                self._job = _KillOnCloseJob(process.pid)
            except OSError:
                self._job = None  # best effort; close() still kills the process

    @classmethod
    def spawn(cls, provider: str, argv: list[str]) -> "TunnelProcess":
        loop = asyncio.get_running_loop()
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return cls(provider, process, loop)

    @property
    def job_protected(self) -> bool:
        return self._job is not None

    def _pump(self) -> None:
        stream = self._process.stdout
        try:
            for raw in iter(stream.readline, b""):
                if not self._scanning:
                    continue  # keep draining so the program never blocks on a full pipe
                line = raw.decode("utf-8", "replace")
                try:
                    self._loop.call_soon_threadsafe(self._queue.put_nowait, line)
                except RuntimeError:  # loop already closed
                    return
        except (OSError, ValueError):
            pass
        finally:
            try:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
            except RuntimeError:
                pass

    async def wait_for_url(self, timeout: float) -> str | None:
        async def scan() -> str | None:
            while True:
                line = await self._queue.get()
                if line is None:
                    return None
                url = public_url_from_line(self.provider, line)
                if url:
                    return url

        try:
            url = await asyncio.wait_for(scan(), timeout)
        except asyncio.TimeoutError:
            return None
        if url:
            self._scanning = False
        return url

    def alive(self) -> bool:
        return self._process.poll() is None

    def close(self) -> None:
        self._scanning = False
        if self._process.poll() is None:
            try:
                self._process.kill()
            except OSError:
                pass
        try:
            self._process.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass
        try:
            if self._process.stdout:
                self._process.stdout.close()
        except OSError:
            pass
        if self._job is not None:
            self._job.close()
            self._job = None
