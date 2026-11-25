"""Core actions: answers, PowerShell, opening things, files, screenshots."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..config import Config
from .base import (
    IS_WINDOWS,
    ActionContext,
    ActionError,
    ActionResult,
    abort_requested,
    expand,
    resolve_path,
    run_powershell,
    truncate,
    unique_destination,
)


# ---------------------------------------------------------------------------
# reply / powershell
# ---------------------------------------------------------------------------

def reply(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    return ActionResult("reply", True, str(params.get("text", "")))


def powershell(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    config = ctx.config
    command = (params.get("command") or "").strip()
    if not command:
        raise ActionError("Empty PowerShell command.")

    lowered = command.lower()
    for pattern in config.blocked_patterns:
        if pattern.lower() in lowered:
            raise ActionError(
                "Refused by the blocklist in config.ini "
                f"([behaviour] blocked_patterns): matched '{pattern}'."
            )

    started = time.perf_counter()
    proc = run_powershell(command, config.command_timeout, config.workspace_root)
    duration = time.perf_counter() - started

    parts: list[str] = []
    if (proc.stdout or "").strip():
        parts.append(proc.stdout.rstrip())
    if (proc.stderr or "").strip():
        parts.append("— stderr —\n" + proc.stderr.rstrip())
    body = "\n\n".join(parts) or "(the command produced no output)"
    return ActionResult(
        "powershell",
        ok=proc.returncode == 0,
        output=truncate(f"exit code: {proc.returncode}\n\n{body}", config.max_output_chars),
        duration=duration,
    )


# ---------------------------------------------------------------------------
# opening things
# ---------------------------------------------------------------------------

def _open_external(target: str) -> None:
    """Hand something to the desktop (kept injectable so tests never open apps)."""
    if IS_WINDOWS:
        os.startfile(target)  # type: ignore[attr-defined]
        return
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen(
        [opener, target],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def open_target(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    config = ctx.config
    target = (params.get("target") or "").strip().strip('"')
    if not target:
        raise ActionError("Nothing to open.")

    expanded = expand(target)
    is_url = target.lower().startswith(("http://", "https://", "www.", "mailto:"))
    path_to_open: str | None = None

    if not is_url:
        candidate = Path(expanded)
        if not candidate.is_absolute() and (os.sep in expanded or "/" in expanded):
            candidate = config.workspace_root / candidate
        if candidate.exists():
            path_to_open = str(candidate)

    started = time.perf_counter()
    try:
        _open_external(path_to_open or expanded)
    except OSError as exc:
        if IS_WINDOWS:
            # Avoid interpolating a target into cmd.exe: shell metacharacters
            # could otherwise turn a simple open into an arbitrary command.
            quoted = expanded.replace("'", "''")
            proc = run_powershell(
                f"Start-Process -FilePath '{quoted}' -ErrorAction Stop",
                min(config.command_timeout, 30), config.workspace_root,
            )
            if proc.returncode != 0:
                raise ActionError(f"Could not open '{target}': {(proc.stderr or '')[:200]}")
        else:
            raise ActionError(
                f"Could not open '{target}': {exc}. Is a desktop session available?"
            ) from exc

    return ActionResult(
        "open", True, f"Opened: {path_to_open or expanded}",
        duration=time.perf_counter() - started,
    )


SAFE_URL_SCHEMES = {"http", "https"}


def open_url(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    raw = (params.get("url") or "").strip()
    if not raw:
        raise ActionError("No URL given.")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in SAFE_URL_SCHEMES:
        raise ActionError(
            f"Refused: only http/https URLs can be opened (got '{parsed.scheme}')."
        )
    if not parsed.netloc:
        raise ActionError(f"That does not look like a valid URL: {raw}")

    started = time.perf_counter()
    try:
        _open_external(raw)
    except OSError as exc:
        raise ActionError(f"Could not open the browser: {exc}") from exc
    return ActionResult(
        "open_url", True, f"Opened in the default browser: {raw}",
        duration=time.perf_counter() - started,
    )


# ---------------------------------------------------------------------------
# screenshots
# ---------------------------------------------------------------------------

def _screenshot_destination(config: Config, save_path: str | None) -> Path:
    if save_path:
        out = resolve_path(save_path, config, "Screenshot path")
        if out.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            out = out.with_suffix(".png")
        return out
    config.screenshots_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return config.screenshots_dir / f"screenshot_{stamp}.png"


def _save_image(image: Any, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() in {".jpg", ".jpeg"}:
        image.convert("RGB").save(out, quality=90)
    else:
        image.save(out)


def take_screenshot(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    config = ctx.config
    started = time.perf_counter()
    try:
        from PIL import ImageGrab
    except ImportError as exc:  # pragma: no cover
        raise ActionError("Pillow is not installed, run: pip install Pillow") from exc

    out = _screenshot_destination(config, params.get("path"))
    grab_kwargs: dict[str, Any] = {"all_screens": True} if IS_WINDOWS else {}
    try:
        image = ImageGrab.grab(**grab_kwargs)
    except Exception as exc:
        raise ActionError(
            f"Screen capture failed: {exc}. "
            "(On Windows this normally works; make sure the PC is not locked.)"
        ) from exc

    _save_image(image, out)
    return ActionResult(
        "screenshot",
        True,
        f"Screenshot saved: {out} ({image.width}×{image.height}, "
        f"{out.stat().st_size / 1024:.0f} KB)",
        files=[out],
        files_as_document=config.screenshot_as_document,
        duration=time.perf_counter() - started,
    )


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------

TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "cp1256", "cp1252", "latin-1")


def read_text_file(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in TEXT_ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ActionError("Could not decode the file as text (is it binary?).")


def read_file(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    config = ctx.config
    started = time.perf_counter()
    target = resolve_path(params.get("path", ""), config, "File path")
    if not target.exists():
        raise ActionError(f"File not found: {target}")
    if target.is_dir():
        raise ActionError(f"That is a folder, not a file: {target}")

    limit_bytes = config.max_file_read_kb * 1024
    size = target.stat().st_size
    if size > limit_bytes:
        raise ActionError(
            f"File is {size / 1024:.0f} KB which is larger than max_file_read_kb "
            f"({config.max_file_read_kb} KB). Raise the limit in config.ini if needed."
        )

    text = read_text_file(target)
    return ActionResult(
        "read_file",
        True,
        f"{target}\n\n{truncate(text, config.max_output_chars)}",
        duration=time.perf_counter() - started,
    )


def write_file(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    config = ctx.config
    started = time.perf_counter()
    target = resolve_path(params.get("path", ""), config, "File path")
    existed = target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    data = "" if params.get("content") is None else str(params.get("content"))
    try:
        target.write_text(data, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise ActionError(f"Could not write {target}: {exc}") from exc

    verb = "Overwrote" if existed else "Created"
    return ActionResult(
        "write_file", True,
        f"{verb} {target} ({len(data)} characters, utf-8).",
        duration=time.perf_counter() - started,
    )


def list_dir(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    config = ctx.config
    started = time.perf_counter()
    raw = params.get("path")
    target = resolve_path(raw, config, "Folder path") if raw else config.workspace_root
    if not target.exists():
        raise ActionError(f"Folder not found: {target}")
    if not target.is_dir():
        raise ActionError(f"Not a folder: {target}")

    try:
        entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError as exc:
        raise ActionError(f"Could not list {target}: {exc}") from exc

    if not entries:
        return ActionResult("list_dir", True, f"{target}\n\n(folder is empty)",
                            duration=time.perf_counter() - started)

    lines = [f"📁 {target}", ""]
    shown = entries[:200]
    for entry in shown:
        try:
            if entry.is_dir():
                lines.append(f"📁 {entry.name}")
            else:
                lines.append(f"📄 {entry.name}  —  {entry.stat().st_size / 1024:.1f} KB")
        except OSError:
            lines.append(f"❓ {entry.name}  —  (unreadable)")
    if len(entries) > len(shown):
        lines.append(f"\n… and {len(entries) - len(shown)} more entries.")

    return ActionResult(
        "list_dir", True, truncate("\n".join(lines), config.max_output_chars),
        duration=time.perf_counter() - started,
    )


# ---------------------------------------------------------------------------
# clipboard / URL helpers shared with the Windows module
# ---------------------------------------------------------------------------

def _copy_into_downloads(config: Config, source: Path) -> Path:
    """Used by tests and by receive_file for collision-free naming."""
    return unique_destination(config.downloads_dir, source.name)


def _check_abort() -> None:
    if abort_requested():
        raise ActionError("Aborted by /panic.")
