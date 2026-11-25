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
  locked down with ``icacls`` (Windows) / ``chmod 600`` (POSIX) so only the
  current user can read it.  The protection actually used is written into the
  file and reported by ``python main.py --check``.

The ``TELEGRAM_BOT_TOKEN`` environment variable always wins over the store and
is never written to disk.
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

def _lock_down(path: Path) -> str:
    """Restrict a file to the current user. Returns the method used."""
    if os.name == "nt":
        user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
        if not user:
            return "none"
        try:
            subprocess.run(
                ["icacls", str(path), "/inheritance:r",
                 "/grant:r", f"{user}:F"],
                capture_output=True, timeout=20, check=False,
            )
            return "icacls"
        except Exception:
            return "none"
    try:
        path.chmod(0o600)
        return "chmod600"
    except OSError:
        return "none"


def _harden_dir(directory: Path) -> None:
    if os.name == "nt":
        user = os.environ.get("USERNAME") or ""
        if user:
            try:
                subprocess.run(
                    ["icacls", str(directory), "/inheritance:r",
                     "/grant:r", f"{user}:(OI)(CI)F"],
                    capture_output=True, timeout=20, check=False,
                )
            except Exception:
                pass
        return
    try:
        directory.chmod(0o700)
    except OSError:
        pass


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
    _lock_down(target)
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
        return "plain file + icacls (current user only)"
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
