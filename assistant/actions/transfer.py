"""File transfer and search actions, always confined to the allowed folders."""

from __future__ import annotations

import fnmatch
import os
import time
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from ..config import normalize_path
from .base import (
    ActionContext,
    ActionError,
    ActionResult,
    os_error_reason,
    effective_roots,
    ensure_inside,
    ensure_not_protected,
    human_bytes,
    resolve_in_roots,
    sanitize_filename,
    truncate,
    unique_destination,
)

#: Safety valves so a search can never hang the bot.
MAX_WALK_ENTRIES = 40000
MAX_WALK_SECONDS = 20.0
DEFAULT_FIND_LIMIT = 50
MAX_FIND_LIMIT = 200


# ---------------------------------------------------------------------------
# send_file (PC → Telegram)
# ---------------------------------------------------------------------------

def send_file(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    config = ctx.config
    started = time.perf_counter()
    raw = params.get("path") or ""
    if not raw.strip():
        raise ActionError("No file path given.")

    path = resolve_in_roots(raw, config, "File path")
    try:
        if not path.exists():
            raise ActionError(f"File not found: {path}")
        if not path.is_file():
            raise ActionError(f"That is not a file: {path}")
        size = path.stat().st_size
    except OSError as exc:
        raise ActionError(f"Could not read the file: {os_error_reason(exc)}.") from None
    limit_bytes = config.max_send_file_mb * 1024 * 1024
    if size > limit_bytes:
        raise ActionError(
            f"File is {human_bytes(size)}, larger than max_send_file_mb "
            f"({config.max_send_file_mb} MB)."
        )

    return ActionResult(
        "send_file",
        True,
        f"Sending {path.name} ({human_bytes(size)}) to Telegram.",
        files=[path],
        files_as_document=True,
        duration=time.perf_counter() - started,
    )


# ---------------------------------------------------------------------------
# receive_file (Telegram → PC)
# ---------------------------------------------------------------------------

def receive_file(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    config = ctx.config
    started = time.perf_counter()

    if ctx.transport is None:
        raise ActionError(
            "This action needs the Telegram connection to download the file; "
            "it can only be approved from the chat where the file was sent."
        )

    file_id = (params.get("file_id") or "").strip()
    if not file_id:
        raise ActionError("No Telegram file id to download.")

    # The size Telegram reported is checked before anything is downloaded. Without
    # it the download would be unbounded, so a missing or invalid size is refused.
    raw_size = (params.get("size") or "").strip()
    if not raw_size.isdigit():
        raise ActionError("Telegram did not report the file size, so the download is refused.")
    declared_size = int(raw_size)
    limit_bytes = config.max_send_file_mb * 1024 * 1024
    if declared_size > limit_bytes:
        raise ActionError(
            f"File is {human_bytes(declared_size)}, larger than "
            f"max_send_file_mb ({config.max_send_file_mb} MB)."
        )

    roots = [normalize_path(p) for p in effective_roots(config)]
    if (params.get("target") or "").strip():
        destination = resolve_in_roots(params["target"], config, "Destination", for_write=True)
    else:
        folder = normalize_path(config.downloads_dir)
        ensure_inside(folder, roots, "Downloads folder")
        folder.mkdir(parents=True, exist_ok=True)
        # Received names are sanitized (no separators, streams or device names).
        destination = unique_destination(folder, sanitize_filename(params.get("file_name", "")))
        ensure_not_protected(destination, "Destination", for_write=True)

    if destination.exists():
        # A reviewed, explicit target must not silently become another path.
        if (params.get("target") or "").strip():
            raise ActionError("Destination already exists; request a new plan with another name.")
        destination = unique_destination(destination.parent, destination.name)

    # Last check before the file is written: the destination must not be a link
    # (os.replace would act on the link itself) and its folder must still be inside
    # the allowed roots.
    if destination.is_symlink():
        raise ActionError("Destination is a link; refusing to replace it.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    ensure_inside(normalize_path(destination.parent), roots, "Destination folder")
    fd, temp_name = tempfile.mkstemp(prefix=".tpa-", suffix=".part", dir=destination.parent)
    os.close(fd)
    partial = Path(temp_name)
    try:
        written = ctx.transport.download_file(file_id, partial)
    except Exception as exc:
        partial.unlink(missing_ok=True)
        raise ActionError(f"Download failed: {exc}") from exc
    if not partial.exists():
        raise ActionError("Download failed: Telegram returned no data.")

    actual = partial.stat().st_size
    limit_bytes = config.max_send_file_mb * 1024 * 1024
    if actual > limit_bytes:
        partial.unlink(missing_ok=True)
        raise ActionError(
            f"Downloaded file is {human_bytes(actual)}, larger than "
            f"max_send_file_mb ({config.max_send_file_mb} MB); deleted."
        )
    os.replace(partial, destination)

    return ActionResult(
        "receive_file",
        True,
        f"Saved {destination.name} ({human_bytes(actual)} → "
        f"{written or actual} bytes) to {destination}",
        duration=time.perf_counter() - started,
    )


# ---------------------------------------------------------------------------
# find_files
# ---------------------------------------------------------------------------

def _matches_name(filename: str, pattern: str) -> bool:
    if not pattern:
        return True
    lowered = filename.lower()
    needle = pattern.lower()
    if any(ch in needle for ch in "*?["):
        return fnmatch.fnmatch(lowered, needle)
    return needle in lowered


def _normalize_ext(ext: str) -> str:
    ext = (ext or "").strip().lower()
    if ext and not ext.startswith("."):
        ext = "." + ext
    return ext


def find_files(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    config = ctx.config
    started = time.perf_counter()

    roots = [normalize_path(p) for p in effective_roots(config)]
    root_param = (params.get("root") or "").strip()
    root = resolve_in_roots(root_param, config, "Search folder") if root_param else roots[0]
    if not root.is_dir():
        raise ActionError(f"Not a folder: {root}")

    name = (params.get("name") or "").strip()
    ext = _normalize_ext(params.get("ext", ""))
    try:
        days = int(params.get("days") or 0)
    except ValueError:
        raise ActionError("'days' must be a number of days.") from None
    try:
        limit = int(params.get("limit") or DEFAULT_FIND_LIMIT)
    except ValueError:
        raise ActionError("'limit' must be a number.") from None
    limit = max(1, min(limit, MAX_FIND_LIMIT))
    cutoff = datetime.now() - timedelta(days=days) if days > 0 else None

    matches: list[tuple[Path, float, int]] = []
    visited = 0
    deadline = time.monotonic() + MAX_WALK_SECONDS
    skipped = 0

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if time.monotonic() > deadline or visited > MAX_WALK_ENTRIES:
            skipped = -1
            break
        # Do not wander into hidden/system/version-control mountains.
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith((".", "$")) and d.lower() not in {"node_modules", "__pycache__"}
        ]
        for filename in filenames:
            visited += 1
            if visited > MAX_WALK_ENTRIES or time.monotonic() > deadline:
                skipped = -1
                break
            if not _matches_name(filename, name):
                continue
            if ext and not filename.lower().endswith(ext):
                continue
            full = Path(dirpath) / filename
            try:
                stat = full.stat()
            except OSError:
                continue
            if cutoff is not None:
                if datetime.fromtimestamp(stat.st_mtime) < cutoff:
                    continue
            matches.append((full, stat.st_mtime, stat.st_size))
            if len(matches) >= limit * 4:
                break
        if skipped == -1 or len(matches) >= limit * 4:
            break

    matches.sort(key=lambda item: item[1], reverse=True)
    shown = matches[:limit]

    if not shown:
        return ActionResult(
            "find_files", True,
            f"🔍 No files matched in {root}"
            + (f" (name~'{name}'" if name else " (")
            + (f", ext={ext}" if ext else "")
            + (f", last {days} days" if days else "") + ").",
            duration=time.perf_counter() - started,
        )

    lines = [
        f"🔍 Found {len(matches)} match(es) in {root} — showing {len(shown)}",
        "",
    ]
    for path, mtime, size in shown:
        stamp = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        try:
            relative = path.relative_to(root)
        except ValueError:
            relative = path
        lines.append(f"📄 {relative}  ·  {human_bytes(size)}  ·  {stamp}")
    if skipped == -1:
        lines.append("\n(search stopped early: time/entry budget reached)")
    if len(matches) > len(shown):
        lines.append(f"… and {len(matches) - len(shown)} more (raise 'limit' to see them)")

    return ActionResult(
        "find_files", True, truncate("\n".join(lines), config.max_output_chars),
        duration=time.perf_counter() - started,
    )
