"""The allow-list for /install and /uninstall (Round 9).

Only winget package IDs listed here can be installed or removed. The built-in list is
``assistant/data/app_allowlist.json``. The owner can add entries on purpose with
``[apps] extra_allowed_apps = name=Publisher.Id`` in ``config.ini``.

Fail closed: a missing or damaged built-in file gives an empty list, so nothing installs.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable, Mapping

BUILTIN_PATH = Path(__file__).resolve().parent / "data" / "app_allowlist.json"

# Short lowercase names the user types, e.g. "7zip" or "notepadpp".
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
# winget package IDs: letters, digits, dot, plus, underscore, hyphen. Never starts with "-".
WINGET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]{1,127}$")


def _valid_entry(name: object, app_id: object) -> bool:
    return (isinstance(name, str) and isinstance(app_id, str)
            and NAME_RE.fullmatch(name) is not None
            and WINGET_ID_RE.fullmatch(app_id) is not None
            and ".." not in app_id)


def load_builtin(path: Path = BUILTIN_PATH) -> dict[str, dict[str, str]]:
    """The shipped list. Unreadable file -> empty. Bad entries are skipped, not trusted."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        apps = data["apps"]
        if not isinstance(apps, dict):
            return {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    result: dict[str, dict[str, str]] = {}
    for name, entry in apps.items():
        if not isinstance(entry, dict):
            continue
        app_id = entry.get("id")
        if not _valid_entry(name, app_id):
            continue
        title = entry.get("title")
        result[name] = {"id": app_id, "title": title if isinstance(title, str) and title else name}
    return result


def parse_extra(raw: str | None, reserved: Iterable[str] = ()) -> dict[str, str]:
    """Parse ``name=Publisher.Id, other=Vendor.Tool``. Raises ValueError with a plain reason."""
    if not raw or not raw.strip():
        return {}
    taken = set(reserved)
    result: dict[str, str] = {}
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, sep, app_id = chunk.partition("=")
        name, app_id = name.strip().lower(), app_id.strip()
        if not sep or not _valid_entry(name, app_id):
            raise ValueError(f"'{chunk[:60]}' is not name=winget.id (lowercase name, letters/digits/dots)")
        if name in taken or name in result:
            raise ValueError(f"'{name}' is listed twice or is already built in")
        result[name] = app_id
    return result


def allowed_apps(extra: Mapping[str, str] | None = None,
                 builtin_path: Path = BUILTIN_PATH) -> dict[str, dict[str, str]]:
    """Built-in entries plus the configured extras, keyed by name."""
    apps = load_builtin(builtin_path)
    for name, app_id in (extra or {}).items():
        if _valid_entry(name, app_id) and name not in apps:
            apps[name] = {"id": app_id, "title": name}
    return apps


def names_text(apps: Mapping[str, object]) -> str:
    return ", ".join(sorted(apps)) if apps else "(none; the allow-list could not be read)"
