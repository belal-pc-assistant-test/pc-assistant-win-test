"""Persistent pause switch (Round 8).

While paused, the bot answers every request with a short notice and starts nothing
new. ``/resume``, ``/panic`` and ``/status`` still work. The state lives in a small
JSON file beside ``memory.json`` (``config.jobs_file.parent``), so a restart keeps
the pause. Only ``/resume`` clears it.

Fail closed: a pause file that exists but cannot be read keeps the bot paused.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

PAUSE_FILE_NAME = "pause.json"

# Commands that still work while paused. Matched against the message text only.
EXEMPT_COMMAND_RE = re.compile(r"^/(resume|panic|status)(@\w+)?(\s|$)", re.IGNORECASE)


def pause_path(jobs_file: Path) -> Path:
    """The pause file sits in the same folder as the jobs file and memory store."""
    return Path(jobs_file).parent / PAUSE_FILE_NAME


def is_exempt(text: str | None) -> bool:
    """True for the commands that must keep working while paused."""
    return bool(EXEMPT_COMMAND_RE.match((text or "").strip()))


def load_pause(path: Path) -> dict[str, Any] | None:
    """Return the saved pause, or None when the bot is not paused.

    A file that is present but unreadable counts as paused, so a damaged file can
    never silently resume the bot.
    """
    path = Path(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"paused": True, "since": "unknown", "by": None, "damaged": True}
    if not isinstance(data, dict) or data.get("paused") is not True:
        return {"paused": True, "since": "unknown", "by": None, "damaged": True}
    return {"paused": True, "since": str(data.get("since", "unknown")),
            "by": data.get("by"), "damaged": False}


def save_pause(path: Path, paused: bool, by: int | None) -> None:
    """Write (or clear) the pause file. Raises OSError when the disk refuses."""
    path = Path(path)
    if not paused:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"paused": True, "since": datetime.now().isoformat(timespec="seconds"), "by": by}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)
