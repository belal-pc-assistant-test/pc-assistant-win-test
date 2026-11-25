"""Shared plumbing for all actions: results, paths, subprocesses, aborts."""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol, runtime_checkable

from .. import token_store
from ..config import Config, normalize_path, path_is_inside

IS_WINDOWS = os.name == "nt"

WINDOWS_PATH_RE = re.compile(r"^([a-zA-Z]:[\\/]|\\\\)")
WINDOWS_DRIVE_RE = re.compile(r"^[a-zA-Z]:")

#: Device names Windows resolves to devices no matter the folder or extension
#: (``CON.txt`` is still the console). Writing to or reading them can hang or
#: hit hardware, so they are refused everywhere.
RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{i}" for i in range(10)}
    | {f"LPT{i}" for i in range(10)}
    # Superscript digits are device names too: "COM\u00b9.txt" is COM1.
    | {f"COM{d}" for d in "\u00b9\u00b2\u00b3"}
    | {f"LPT{d}" for d in "\u00b9\u00b2\u00b3"}
)

#: Files that hold credentials. File actions never read, send, list or
#: overwrite these, even inside an allowed folder.
PROTECTED_FILE_NAMES = frozenset({"token.json", "token.txt", ".env"})

#: Autostart folder (normalised to backslashes, lower case); anything written
#: here runs at the next sign-in.
STARTUP_FOLDER_MARKER = "\\start menu\\programs\\startup"

#: Explicit paths the open action refuses to launch (these run code directly).
SCRIPT_EXTENSIONS = frozenset({
    ".bat", ".cmd", ".com", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf",
    ".wsh", ".hta", ".reg", ".scr", ".msi", ".msp", ".lnk", ".url", ".chm", ".cpl",
})

#: URI schemes that are not plain web links (``ms-msdt:``, ``file:``, ``search-ms:``...).
URI_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]+:")


# ---------------------------------------------------------------------------
# Errors and results
# ---------------------------------------------------------------------------

class ActionError(RuntimeError):
    """Raised when a step cannot run (bad input, blocked, missing tool...)."""


@dataclass
class ActionResult:
    action: str
    ok: bool
    output: str = ""
    files: list[Path] = field(default_factory=list)
    files_as_document: bool = True
    duration: float = 0.0
    photo: bytes | None = None  # in-memory screenshot; never written to disk

    @property
    def has_files(self) -> bool:
        return bool(self.files)


@runtime_checkable
class Transport(Protocol):
    """What an action may need from the Telegram side (file downloads)."""

    def download_file(self, file_id: str, destination: Path) -> int:
        """Download ``file_id`` to ``destination``; return bytes written."""


@dataclass
class ActionContext:
    """Everything a handler needs besides its own params."""

    config: Config
    transport: Transport | None = None
    jobs: Any = None          # assistant.jobs.JobStore (set by the bot)
    chat_id: int | None = None
    capture_allowed: bool = False  # set only after approval + visible indicator
    capture_window_handle: int = 0


# ---------------------------------------------------------------------------
# Abort support (/panic)
# ---------------------------------------------------------------------------

class ProcessRegistry:
    """Keeps track of live child processes so /panic can terminate them."""

    def __init__(self) -> None:
        self._procs: set[subprocess.Popen] = set()
        self._lock = threading.Lock()

    def add(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.add(proc)

    def discard(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.discard(proc)

    def kill_all(self) -> int:
        with self._lock:
            procs = list(self._procs)
        killed = 0
        for proc in procs:
            try:
                if proc.poll() is None:
                    proc.kill()
                    killed += 1
            except Exception:
                continue
        return killed

    @property
    def running(self) -> int:
        with self._lock:
            return sum(1 for p in self._procs if p.poll() is None)


REGISTRY = ProcessRegistry()
_ABORT = threading.Event()


def request_abort() -> None:
    """Set by /panic: stop the current plan and kill its child processes."""
    _ABORT.set()
    REGISTRY.kill_all()


def clear_abort() -> None:
    _ABORT.clear()


def abort_requested() -> bool:
    return _ABORT.is_set()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n\n… output truncated ({len(text)} characters total)."


def expand(raw: str) -> str:
    raw = (raw or "").strip().strip('"').strip("'")
    return os.path.expandvars(os.path.expanduser(raw))


def effective_roots(config: Config) -> list[Path]:
    """Folders that file actions may touch.

    ``[paths] allowed_roots`` when set, otherwise the working folder. File
    transfer actions always stay inside these folders. Every root is
    returned in normalized form (see :func:`normalize_path`) so checks and
    reported paths never mix short/long or differently-cased spellings.
    """
    roots = list(config.allowed_roots) or [config.workspace_root]
    return [normalize_path(root) for root in roots]


def ensure_inside(path: Path, roots: Iterable[Path], what: str = "Path") -> Path:
    """Return ``path`` in normalized form once it sits inside one of ``roots``.

    Both sides go through :func:`normalize_path`, so an 8.3 short-form root
    (``C:\\Users\\RUNNER~1\\…``) accepts the long-form path of the very same
    folder (``C:\\Users\\runneradmin\\…``) on Windows.
    """
    normalized = normalize_path(path)
    for root in roots:
        if path_is_inside(normalized, normalize_path(root)):
            return normalized
    pretty = ", ".join(str(normalize_path(root)) for root in roots)
    raise ActionError(f"{what} is outside the allowed folders ({pretty}): {normalized}")


def check_path_syntax(cleaned: str, what: str = "Path") -> None:
    """Refuse path spellings that leave the local disk namespace.

    * UNC and device paths (``\\\\server\\share``, ``\\\\?\\``, ``\\\\.\\``, ``//server``):
      opening them can make Windows send NTLM credentials to a remote host.
    * Alternate data streams (``file.txt:stream``): hidden content that
      listings and size checks do not show.
    * Reserved device names in any component (``CON``, ``NUL``, ``COM1``...).
    """
    if "\x00" in cleaned:
        raise ActionError(f"{what} contains a NUL character.")
    # Windows treats "/" like "\\", so "/\\server\\share" is a UNC path just like
    # "\\\\server\\share". Two leading separators mean UNC or a device (\\?\, \\.\),
    # and "\\??\\" is the NT namespace (\\??\\UNC\\... is a network path too).
    normalized_sep = cleaned.replace("/", "\\")
    if normalized_sep.startswith("\\\\") or re.match(r"^\\\?\?\\", normalized_sep):
        raise ActionError(f"{what}: network (UNC) and device paths are not allowed.")
    body = cleaned[2:] if WINDOWS_DRIVE_RE.match(cleaned) else cleaned
    for part in re.split(r"[\\/]", body):
        if not part:
            continue
        if ":" in part:
            raise ActionError(f"{what}: alternate data streams (':') are not allowed.")
        stem = part.split(".", 1)[0].rstrip(" ").upper()
        if stem in RESERVED_DEVICE_NAMES:
            raise ActionError(f"{what}: '{part}' is a reserved Windows device name.")


def ensure_not_protected(path: Path, what: str = "Path", *, for_write: bool = False) -> None:
    """Refuse credential files, the token store and (for writes) autostart folders.

    ``path`` must already be normalized, so symlinks and junctions that point
    at a protected file are caught by their real target.
    """
    # Windows ignores trailing dots and spaces, so "token.txt." is token.txt, and the
    # 8.3 short form of token.txt is TOKEN~1.TXT. Both are matched here.
    name = path.name.rstrip(" .").casefold()
    if name in PROTECTED_FILE_NAMES or re.fullmatch(r"token~\d+(\..*)?", name):
        raise ActionError(f"{what} is a protected credential file and cannot be used.")
    try:
        store_dir = normalize_path(token_store.token_path().parent)
    except Exception:  # no home folder: the name check still applies
        store_dir = None
    if store_dir is not None and path_is_inside(path, store_dir):
        raise ActionError(f"{what} is inside the assistant's private token folder.")
    if for_write:
        lowered = str(path).lower().replace("/", "\\")
        if STARTUP_FOLDER_MARKER in lowered:
            raise ActionError(f"{what} is in the Windows Startup folder; writing there is refused.")


def ps_literal(text: str) -> str:
    """A PowerShell expression that yields ``text`` exactly.

    Pasting text into a script is unsafe: PowerShell treats several Unicode
    quote characters (U+2018, U+2019, U+201A, U+201B) as string delimiters, so
    ASCII-only escaping can be broken out of. Base64 contains no quotes or
    special characters, so the value stays inert whatever it contains.
    """
    encoded = base64.b64encode((text or "").encode("utf-8")).decode("ascii")
    return ("([System.Text.Encoding]::UTF8.GetString("
            f"[System.Convert]::FromBase64String('{encoded}')))")


def resolve_path(raw: str, config: Config, what: str = "Path", *, for_write: bool = False) -> Path:
    """Turn user/model input into an absolute, allow-list-checked path.

    Syntax (UNC, device names, streams), credential files and autostart
    folders are refused in every case. The allowed roots are enforced when
    configured. Symlinks and junctions are resolved first, so the checks apply
    to the real target.
    """
    cleaned = expand(raw)
    if not cleaned:
        return config.workspace_root
    check_path_syntax(cleaned, what)
    path = Path(cleaned)
    if not path.is_absolute() and not WINDOWS_PATH_RE.match(cleaned):
        path = config.workspace_root / path
    path = normalize_path(path)
    if config.allowed_roots:
        ensure_inside(path, config.allowed_roots, what)
    ensure_not_protected(path, what, for_write=for_write)
    return path


def resolve_in_roots(raw: str, config: Config, what: str = "Path", *, for_write: bool = False) -> Path:
    """Like :func:`resolve_path`, but always confined to the effective roots."""
    cleaned = expand(raw)
    roots = effective_roots(config)
    if not cleaned:
        return roots[0]
    check_path_syntax(cleaned, what)
    path = Path(cleaned)
    if not path.is_absolute() and not WINDOWS_PATH_RE.match(cleaned):
        path = roots[0] / path
    resolved = ensure_inside(path, roots, what)
    ensure_not_protected(resolved, what, for_write=for_write)
    return resolved


def sanitize_filename(name: str, fallback: str = "download.bin") -> str:
    """Strip directory parts and characters Windows cannot store."""
    raw = (name or "").replace("\\", "/").split("/")[-1].strip()
    raw = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", raw)
    raw = raw.strip(" .")
    if not raw or raw in {".", ".."}:
        return fallback
    # "CON.txt", "nul" or "COM1.log" name devices on Windows; prefix them.
    if raw.split(".", 1)[0].rstrip(" ").upper() in RESERVED_DEVICE_NAMES:
        raw = "_" + raw
    return raw[:180]


def unique_destination(folder: Path, filename: str) -> Path:
    """Return a non-existing path inside ``folder`` (adds " (1)" when needed)."""
    candidate = folder / filename
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for index in range(1, 1000):
        alternative = folder / f"{stem} ({index}){suffix}"
        if not alternative.exists():
            return alternative
    raise ActionError(f"Could not find a free filename in {folder}")


def run_process(
    argv: list[str],
    timeout: float,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run a child process that /panic can kill; never leaves zombies behind."""
    if abort_requested():
        raise ActionError("Aborted by /panic before starting.")
    kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.DEVNULL,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "cwd": str(cwd) if cwd and Path(cwd).is_dir() else None,
        "env": env,
    }
    try:
        proc = subprocess.Popen(argv, **kwargs)  # type: ignore[arg-type]
    except OSError as exc:
        raise ActionError(f"Could not start process: {exc}") from exc

    REGISTRY.add(proc)
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise ActionError(
                f"Command timed out after {timeout:.0f}s and was killed."
            ) from None
    finally:
        REGISTRY.discard(proc)

    if abort_requested():
        raise ActionError("Aborted by /panic.")
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def powershell_exe() -> list[str]:
    """Executable + flags to run a PowerShell command."""
    if IS_WINDOWS:
        for candidate in ("powershell", "pwsh"):
            exe = shutil.which(candidate)
            if exe:
                return [
                    exe, "-NoProfile", "-NonInteractive", "-Command",
                ]
        raise ActionError("PowerShell was not found on this system.")
    pwsh = shutil.which("pwsh")
    if pwsh:
        return [pwsh, "-NoProfile", "-NonInteractive", "-Command"]
    raise ActionError("PowerShell is only available on Windows.")


def run_powershell(command: str, timeout: float, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run PowerShell with UTF-8 output forced (so Arabic survives)."""
    wrapped = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "$OutputEncoding = [System.Text.Encoding]::UTF8; "
        "try { chcp 65001 > $null } catch {}; "
        f"{command}"
    )
    return run_process(powershell_exe() + [wrapped], timeout, cwd)


def windows_only(feature: str) -> ActionError:
    return ActionError(f"{feature} is only available on Windows.")


def hostname_and_ip() -> tuple[str, str]:
    import socket

    try:
        host = socket.gethostname()
    except Exception:
        host = "?"
    ip = "?"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.5)
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
    except Exception:
        try:
            ip = socket.gethostbyname(host)
        except Exception:
            ip = "?"
    return host, ip


def human_bytes(size: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def python_executable() -> str:
    return sys.executable or "python"
