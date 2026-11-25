"""Secure-ish storage for the bot token + first-run setup.

Nothing secret is shipped in the project files.  On the very first run the
assistant asks for the token with hidden input, checks it against Telegram
``getMe``, then stores it **outside the project** in the per-user application
data folder:

    Windows :  %APPDATA%\\TelegramPCAssistant\\token.json
    other   :  ~/.config/TelegramPCAssistant/token.json

Protection:

* **DPAPI** (Windows, via ``ctypes``): the token is encrypted with
  ``CryptProtectData`` so only the *same Windows user on the same machine* can
  decrypt it.  No key is stored anywhere.
* **Fallback**: if DPAPI is unavailable the file is written as plain JSON and
  locked down to the current user (an owner-only access list on Windows, set
  and read back by PowerShell / ``chmod 600`` on POSIX) so only the current
  user can read it.  The protection actually used is written into the
  file and reported by ``python main.py --check``.

Lookup order (see ``config.py``): project-root ``token.txt``, then the
``TELEGRAM_BOT_TOKEN`` environment variable, then this stored token. The
environment variable is never written to disk.
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import re
import subprocess
import sys
from pathlib import Path

APP_DIR_NAME = "TelegramPCAssistant"
TOKEN_FILE_NAME = "token.json"
TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_\-]{20,}$")


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

def store_dir() -> Path:
    """Per-user folder for the stored token (overridable for tests)."""
    override = os.environ.get("TPA_TOKEN_STORE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_DIR_NAME


def token_path() -> Path:
    return store_dir() / TOKEN_FILE_NAME


# ---------------------------------------------------------------------------
# DPAPI (Windows only, via ctypes)
# ---------------------------------------------------------------------------

def _dpapi(data: bytes, encrypt: bool) -> bytes:
    """Encrypt/decrypt with Windows DPAPI, tied to the current user."""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)      # type: ignore[attr-defined]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)    # type: ignore[attr-defined]

    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()

    if encrypt:
        ok = crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0,
            ctypes.byref(blob_out),
        )
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0,
            ctypes.byref(blob_out),
        )
    if not ok:
        code = ctypes.get_last_error()
        raise OSError(code, f"DPAPI {'encrypt' if encrypt else 'decrypt'} failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def dpapi_available() -> bool:
    """True if we can protect data with DPAPI on this machine."""
    if os.name != "nt":
        return False
    try:
        probe = b"telegram-pc-assistant"
        return _dpapi(probe, encrypt=True) != b""
    except Exception:
        return False


# ---------------------------------------------------------------------------
# File permissions
# ---------------------------------------------------------------------------

_SID_RE = re.compile(r"S-\d+(?:-\d+)+")


def _on_windows() -> bool:
    """Single seam for the platform check, so tests can exercise the Windows branch."""
    return os.name == "nt"


def _current_user_sid() -> str | None:
    """SID of the signed-in Windows user, or None.

    The SID is used in access-list entries instead of the account name: names
    can contain spaces, Arabic or other non-ASCII letters, and characters that
    permission syntax would misread. SIDs are the same in every display language.
    """
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = (proc.stdout or "").strip()
    return value if proc.returncode == 0 and _SID_RE.fullmatch(value) else None


# The access list is set and read back by one PowerShell script. Paths and SIDs
# arrive in environment variables (never on the command line), so Arabic letters,
# spaces and quotes reach Windows unchanged. The script:
#   * turns off inheritance and removes every entry (inherited or explicit), so the
#     SYSTEM, Administrators and OWNER RIGHTS entries that a new file receives from
#     its creator's default access list are removed too;
#   * grants full control to the current user only, and makes that user the owner;
#   * prints a JSON report of what is on the object afterwards.
# Python decides success from that report (see acl_is_owner_only), so the check is
# the same on every Windows display language.
_ACL_HEADER = r"""
$ErrorActionPreference = 'Stop'
$path = $env:TPA_ACL_PATH
$sid = New-Object System.Security.Principal.SecurityIdentifier($env:TPA_ACL_SID)
$acl = Get-Acl -LiteralPath $path
"""
_ACL_APPLY = r"""
$acl.SetAccessRuleProtection($true, $false)
foreach ($entry in @($acl.Access)) { [void]$acl.RemoveAccessRule($entry) }
if ($env:TPA_ACL_DIR -eq '1') {
    $inherit = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
} else {
    $inherit = [System.Security.AccessControl.InheritanceFlags]::None
}
$grant = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, [System.Security.AccessControl.FileSystemRights]::FullControl, $inherit, [System.Security.AccessControl.PropagationFlags]::None, [System.Security.AccessControl.AccessControlType]::Allow)
[void]$acl.AddAccessRule($grant)
$acl.SetOwner($sid)
Set-Acl -LiteralPath $path -AclObject $acl
$acl = Get-Acl -LiteralPath $path
"""
_ACL_REPORT = r"""
$entries = @()
foreach ($entry in $acl.Access) {
    try { $value = $entry.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value }
    catch { $value = [string]$entry.IdentityReference }
    $entries += [pscustomobject]@{ sid = $value; inherited = [bool]$entry.IsInherited }
}
$owner = ''
try { $owner = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value } catch { $owner = '' }
[pscustomobject]@{ owner = $owner; entries = @($entries) } | ConvertTo-Json -Compress -Depth 4
"""
_ACL_READ_SCRIPT = _ACL_HEADER + _ACL_REPORT
_ACL_APPLY_SCRIPT = _ACL_HEADER + _ACL_APPLY + _ACL_REPORT


def _last_json_object(text: str) -> dict | None:
    for line in reversed((text or "").strip().splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        return value if isinstance(value, dict) else None
    return None


def acl_is_owner_only(report: dict | None, sid: str) -> bool:
    """True when the report lists exactly one entry, the user's own, and the user owns the object.

    An inherited entry counts as a failure, and so does an empty list, so a missing
    report can never pass.
    """
    if not isinstance(report, dict) or not sid:
        return False
    entries = report.get("entries")
    if not isinstance(entries, list) or not entries:
        return False
    if report.get("owner") != sid:
        return False
    return all(isinstance(entry, dict) and entry.get("sid") == sid and entry.get("inherited") is False
               for entry in entries)


def _run_acl_script(script: str, target: Path, sid: str, *, directory: bool) -> dict | None:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    env = dict(os.environ, TPA_ACL_PATH=str(target), TPA_ACL_SID=sid,
               TPA_ACL_DIR="1" if directory else "0")
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, check=False, env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return _last_json_object(proc.stdout)


def windows_acl_report(target: Path) -> dict | None:
    """Read-only: who is on the access list of ``target`` and who owns it (Windows only)."""
    sid = _current_user_sid()
    if not sid:
        return None
    return _run_acl_script(_ACL_READ_SCRIPT, target, sid, directory=False)


def _apply_owner_only(target: Path, sid: str, *, directory: bool) -> bool:
    report = _run_acl_script(_ACL_APPLY_SCRIPT, target, sid, directory=directory)
    return acl_is_owner_only(report, sid)


def _lock_down(path: Path, *, is_windows: bool | None = None) -> str:
    """Restrict a file to the current user. Returns "acl", "chmod600" or "none".

    "acl" means the read-back showed only the current user, as owner and as the sole
    entry (see :func:`acl_is_owner_only`). Anything else is reported as "none", and
    the caller must not treat the file as protected.
    """
    if is_windows is None:
        is_windows = _on_windows()
    if is_windows:
        sid = _current_user_sid()
        if not sid:
            return "none"
        return "acl" if _apply_owner_only(path, sid, directory=False) else "none"
    try:
        path.chmod(0o600)
        return "chmod600"
    except OSError:
        return "none"


def _harden_dir(directory: Path, *, is_windows: bool | None = None) -> bool:
    """Make a folder owner-only, so files created inside it inherit that. Returns success."""
    if is_windows is None:
        is_windows = _on_windows()
    if is_windows:
        sid = _current_user_sid()
        return bool(sid) and _apply_owner_only(directory, sid, directory=True)
    try:
        directory.chmod(0o700)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def valid_token_format(token: str) -> bool:
    return bool(TOKEN_RE.match((token or "").strip()))


def mask_token(token: str | None) -> str:
    """Never print a full token: 8123456789:AAH…UVW."""
    if not token:
        return "(none)"
    token = token.strip()
    if ":" not in token:
        return token[:4] + "…"
    head, _, tail = token.partition(":")
    return f"{head}:{tail[:3]}…{tail[-3:]}" if len(tail) > 6 else f"{head}:…"


def load_stored_token(path: Path | None = None) -> str | None:
    """Read the saved token, decrypting when needed. Returns None if absent/broken."""
    target = path or token_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None

    protection = str(data.get("protection", "plain"))
    payload = data.get("token", "")
    if not isinstance(payload, str) or not payload:
        return None
    try:
        blob = base64.b64decode(payload)
    except Exception:
        return None

    if protection == "dpapi":
        try:
            token = _dpapi(blob, encrypt=False).decode("utf-8").strip()
        except Exception:
            return None
    else:
        token = blob.decode("utf-8", errors="replace").strip()
    return token or None


def save_token(token: str, path: Path | None = None) -> Path:
    """Persist a token, encrypted with DPAPI when possible."""
    token = (token or "").strip()
    if not token:
        raise ValueError("Refusing to save an empty token.")
    if not valid_token_format(token):
        raise ValueError("That does not look like a Telegram bot token (digits:secret).")

    target = path or token_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    _harden_dir(target.parent)

    protection = "plain"
    blob = token.encode("utf-8")
    if dpapi_available():
        try:
            blob = _dpapi(blob, encrypt=True)
            protection = "dpapi"
        except Exception:
            blob = token.encode("utf-8")
            protection = "plain"

    payload = {
        "version": 1,
        "protection": protection,
        "created": _now(),
        "token": base64.b64encode(blob).decode("ascii"),
    }
    # Write with an exclusive, owner-only file handle first, then lock down.
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    method = _lock_down(target)
    if protection == "plain" and _on_windows() and method != "acl":
        # Fail closed: a readable plain-text token must not be left behind on a
        # Windows account where the owner-only restriction could not be applied.
        try:
            target.unlink()
        except OSError:
            pass
        raise RuntimeError("Could not restrict the token file to your Windows account, "
                           "so the token was not saved. Run the command again from an "
                           "account that can change file permissions.")
    return target


def protection_of(path: Path | None = None) -> str:
    """Human readable description of how the stored token is protected."""
    target = path or token_path()
    if not target.exists():
        return "no stored token"
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return "unreadable file"
    protection = str(data.get("protection", "plain"))
    if protection == "dpapi":
        return "DPAPI (encrypted, current Windows user only)"
    if os.name == "nt":
        return "plain file, access list limited to the current user"
    return "plain file + chmod 600"


def delete_token(path: Path | None = None) -> bool:
    target = path or token_path()
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return False


def _now() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Online validation
# ---------------------------------------------------------------------------

def validate_token(token: str, timeout: float = 20) -> tuple[bool, str, dict | None]:
    """Ask Telegram ``getMe`` about a token.

    Returns ``(ok, message, result_dict_or_None)``.
    """
    import requests

    token = (token or "").strip()
    if not valid_token_format(token):
        return False, "That does not look like a bot token (expected 123456789:AA...).", None
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getMe", timeout=timeout
        )
    except requests.exceptions.ConnectionError:
        return False, "Could not reach api.telegram.org (no internet connection?).", None
    except requests.exceptions.Timeout:
        return False, f"api.telegram.org did not answer within {timeout:.0f}s.", None
    except Exception as exc:  # pragma: no cover - unexpected transport error
        return False, f"Telegram request failed: {str(exc).replace(token, '[REDACTED]')}", None

    try:
        data = resp.json()
    except ValueError:
        return False, f"Telegram returned a non-JSON reply (HTTP {resp.status_code}).", None

    if data.get("ok"):
        result = data.get("result") or {}
        username = result.get("username", "?")
        return True, f"Token valid: @{username} (id {result.get('id')}).", result
    return False, f"Telegram rejected the token: {str(data.get('description', 'unknown error')).replace(token, '[REDACTED]')}", None


# ---------------------------------------------------------------------------
# Interactive first-run setup
# ---------------------------------------------------------------------------

def ask_for_token_interactively(max_attempts: int = 3, validate: bool = True) -> str | None:
    """Prompt for the token with hidden input, validate it, and store it.

    Returns the saved token, or ``None`` if the user gave up / no terminal.
    """
    if not sys.stdin or not sys.stdin.isatty():
        print(
            "No interactive terminal available, so the token cannot be requested.\n"
            "Run  python main.py --set-token  in a console window, or set the\n"
            "TELEGRAM_BOT_TOKEN environment variable.",
            file=sys.stderr,
        )
        return None

    print("\n" + "=" * 62)
    print(" First run: the bot token is needed.")
    print(" Get it from @BotFather in Telegram -> /mybots -> API Token.")
    print(" It is stored encrypted for your Windows user only:")
    print(f"   {token_path()}")
    print(" Input is hidden (nothing is echoed, not even dots).")
    print("=" * 62)

    for attempt in range(1, max_attempts + 1):
        try:
            token = getpass.getpass("\nPaste bot token (hidden): ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled.")
            return None
        if not token:
            print("Nothing entered.")
            if attempt < max_attempts:
                continue
            return None

        ok, message, _ = validate_token(token) if validate else (True, "not verified", None)
        if not ok:
            print(f"❌ {message}")
            if attempt < max_attempts:
                print("Try again.\n")
            continue

        print(f"✅ {message}")
        try:
            saved_to = save_token(token)
        except (OSError, ValueError) as exc:
            print(f"⚠️ Could not save the token ({exc}).")
            print("   It will be used for this run only.")
            return token
        print(f"💾 Saved to {saved_to}")
        print(f"   Protection: {protection_of()}")
        return token

    print("\nToo many failed attempts. Get a fresh token from @BotFather and rerun.")
    return None
