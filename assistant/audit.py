"""Local, rotating audit log.

Every request, plan, approval/denial and action result is written here as one
JSON object per line, so the file is both human readable and greppable.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger("assistant")
_configured = False


_secrets: set[str] = set()


def register_secret(secret: str) -> None:
    if secret:
        _secrets.add(secret)


def redact_secret(text: str) -> str:
    for secret in tuple(_secrets):
        text = text.replace(secret, "[REDACTED]")
    return text


def registered_secrets() -> tuple[str, ...]:
    """The secrets that :func:`redact_secret` removes (read-only snapshot)."""
    return tuple(_secrets)


class SafeFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Format exception tracebacks first so secrets are also removed there.
        return redact_secret(super().format(record))


def setup(log_file: Path, level: str = "INFO") -> None:
    """Attach a rotating file handler (and console output) to the logger."""
    global _configured
    if _configured:
        return

    log_file.parent.mkdir(parents=True, exist_ok=True)

    formatter = SafeFormatter(
        fmt="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # A log file another program holds open (Windows sharing violation) must not
    # stop the bot from starting. Fall back to console output and say so.
    try:
        file_handler = RotatingFileHandler(
            log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        )
    except OSError:
        file_handler = None

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.setLevel(getattr(logging, level, logging.INFO))

    _LOGGER.setLevel(logging.DEBUG)
    if file_handler is not None:
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)
        _LOGGER.addHandler(file_handler)
    else:
        print("Warning: the log file is locked or unavailable (is another program "
              "holding it open?). Logging to the console only.", file=sys.stderr)
    _LOGGER.addHandler(console)
    _LOGGER.propagate = False
    _configured = True


def install_root_redaction(stream=None, level: int = logging.WARNING) -> logging.Handler:
    """Route root-logger output (library warnings) through the same redaction.

    Without a handler on the root logger, Python falls back to printing
    WARNING+ records unredacted on stderr. Installing this handler makes every
    such message pass through :func:`redact_secret`. Idempotent.
    """
    root = logging.getLogger()
    for existing in root.handlers:
        if getattr(existing, "_tpa_redacting", False):
            return existing
    handler = logging.StreamHandler(stream)
    handler.setLevel(level)
    handler.setFormatter(SafeFormatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    handler._tpa_redacting = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    return handler


def close() -> None:
    """Flush and close every log handler and allow re-configuration later.

    Windows keeps deleted-but-open files locked (WinError 32), so the bot
    calls this on shutdown and tests call it in ``tearDown`` before removing
    the temporary log folder.
    """
    global _configured
    for handler in tuple(_LOGGER.handlers):
        try:
            handler.flush()
            handler.close()
        except Exception:  # never fail shutdown because of logging
            pass
        _LOGGER.removeHandler(handler)
    _configured = False


# Audit metadata only: never store requests, transcripts, notes, commands,
# output, exception text, paths or other potentially secret-bearing content.
_SAFE_FIELDS = {'user_id', 'chat_id', 'step', 'ok', 'duration', 'failed', 'aborted', 'killed', 'jobs',
                'minutes', 'frames', 'inputs', 'viewers', 'rejected'}
# Text is allowed only for a few labels that our own code produces (reasons, provider
# names). Anything that is not a short plain label is dropped, so URLs, secrets or
# request text can never reach the log through these fields.
_SAFE_TEXT_FIELDS = {'reason', 'provider'}
_SAFE_TEXT_RE = re.compile(r'[a-z][a-z .\-]{0,39}')  # lowercase labels only; tokens never match

def event(kind: str, **fields: Any) -> None:
    payload = {"event": kind, "ts": datetime.now().isoformat(timespec="seconds")}
    payload.update({k: v for k, v in fields.items() if k in _SAFE_FIELDS and isinstance(v, (int, float, bool))})
    payload.update({k: v for k, v in fields.items()
                    if k in _SAFE_TEXT_FIELDS and isinstance(v, str) and _SAFE_TEXT_RE.fullmatch(v)})
    _LOGGER.info(json.dumps(payload, ensure_ascii=False))


def warning(message: str, **fields: Any) -> None:
    event("warning", **fields)


def error(message: str, exc_info: bool = False, **fields: Any) -> None:
    # Tracebacks and exception messages may contain request data or credentials,
    # so only the event name and whitelisted labels (e.g. reason=) are logged.
    event("error", **fields)


def tail(log_file: Path, lines: int = 60) -> str:
    """Return the last ``lines`` lines of the log file (for the /logs command)."""
    try:
        with log_file.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            block = 8192
            data = b""
            while size > 0 and data.count(b"\n") <= lines:
                step = min(block, size)
                size -= step
                fh.seek(size)
                data = fh.read(step) + data
        text = data.decode("utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])
    except OSError as exc:  # pragma: no cover - filesystem edge case
        return f"(could not read log: {exc})"
