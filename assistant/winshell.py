"""How the bot starts Windows PowerShell (Round 9).

The bot may itself run inside PowerShell 7 (pwsh). PowerShell 7 sets ``PSModulePath``
to its own module folders. If a child Windows PowerShell 5 inherits that value, it
cannot load ``Microsoft.PowerShell.Security`` and ``Get-Acl`` fails. Two rules fix it:

* start Windows PowerShell by its full path under ``%SystemRoot%``, and fall back to
  the bare name ``powershell`` only when that file does not exist;
* remove ``PSModulePath`` (Windows PowerShell rebuilds its default) and
  ``PSExecutionPolicyPreference`` from the child's environment.

Failure text is reduced to a short, sanitized reason, so a failure is diagnosable
without ever showing the bot token or a full path.
"""
from __future__ import annotations

import base64
import html
import os
import re
from typing import Iterable, Mapping

from . import audit

# Environment variables that belong to the parent PowerShell and must not reach the child.
DROPPED_ENV_NAMES = frozenset({"PSMODULEPATH", "PSEXECUTIONPOLICYPREFERENCE"})

DEFAULT_REASON_LIMIT = 200

_CLIXML_START = "#< CLIXML"
_ERROR_STREAM = re.compile(r'<S S="Error">(.*?)</S>', re.S)
_XML_CHAR = re.compile(r"_x([0-9A-Fa-f]{4})_")
# A path runs to the end of its line, so a folder name with spaces cannot leave a tail behind.
_WINDOWS_PATH = re.compile(r"\b[A-Za-z]:\\[^\r\n]*|\\\\[^\r\n]+")
_POSIX_PATH = re.compile(r"(?<![\w.])/[\w.\-]+(?:/[^\r\n]*)?")
_TOKEN_SHAPE = re.compile(r"\b\d{6,}:[A-Za-z0-9_\-]{20,}\b")


def windows_powershell_path(environ: Mapping[str, str] | None = None) -> str:
    """Full path of Windows PowerShell 5, or the bare name when that file is missing."""
    env = os.environ if environ is None else environ
    root = env.get("SystemRoot") or env.get("SYSTEMROOT") or env.get("WINDIR")
    if root:
        candidate = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        if os.path.isfile(candidate):
            return candidate
    return "powershell"


def child_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment for a child PowerShell, without the parent's module settings."""
    source = os.environ if base is None else base
    return {name: value for name, value in source.items() if name.upper() not in DROPPED_ENV_NAMES}


def decode_output(data: object) -> str:
    """Text from captured process output. Never raises; accepts str, bytes or None."""
    if isinstance(data, str):
        return data
    if isinstance(data, (bytes, bytearray)):
        return bytes(data).decode("utf-8", errors="replace")
    return ""


def _unescape_clixml(text: str) -> str:
    text = html.unescape(text)
    return _XML_CHAR.sub(lambda m: chr(int(m.group(1), 16)), text)


def clixml_to_text(raw: object) -> str:
    """Stderr text with CLIXML progress noise removed. Other text is returned unchanged."""
    text = decode_output(raw)
    if _CLIXML_START in text:
        head = text.split(_CLIXML_START, 1)[0]
        errors = [_unescape_clixml(match) for match in _ERROR_STREAM.findall(text)]
        text = "\n".join([head, *errors])
    return text


_TRACEBACK_LINE = re.compile(r'Traceback \(most recent call last\):|File "')


def sanitize_failure(raw: object, limit: int = DEFAULT_REASON_LIMIT,
                     keep: Iterable[str] = ()) -> str:
    """A short, safe reason from PowerShell stderr.

    * CLIXML progress noise is dropped; only the error stream text is kept.
    * Registered secrets, token-shaped strings, Windows paths and POSIX paths are replaced
      with ``<path>``. The one exception is a value in ``keep``: text the user typed
      (for example the file they asked to open), which is shown as typed, unless it
      contains a secret or token, which is always redacted.
    * The result is one line of at most ``limit`` characters, and never empty.
    """
    text = clixml_to_text(raw)
    kept: list[str] = []
    for value in keep:
        value = str(value or "")
        if value and value in text:
            # Mask the user's value so the path rules below cannot touch it.
            text = text.replace(value, f"\ue000{len(kept)}\ue001")
            kept.append(_TOKEN_SHAPE.sub("[REDACTED]", audit.redact_secret(value)))
    lines: list[str] = []
    for line in text.splitlines():
        line = audit.redact_secret(line.strip())
        if not line or _TRACEBACK_LINE.match(line):
            # Python traceback frames are not a reason a user can act on; keep only
            # the exception line that follows them.
            continue
        line = _TOKEN_SHAPE.sub("[REDACTED]", line)
        line = _WINDOWS_PATH.sub("<path>", line)
        line = _POSIX_PATH.sub("<path>", line)
        lines.append(line)
        if len(lines) == 3:
            break
    text = " ".join(lines)
    for index, value in enumerate(kept):
        text = text.replace(f"\ue000{index}\ue001", value)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        text = "PowerShell gave no details"
    if len(text) > limit:
        text = text[: max(limit - 1, 0)].rstrip() + "…"
    return text


def encode_script(script: str) -> str:
    """Base64 of UTF-16LE, the form ``powershell -EncodedCommand`` expects."""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")
