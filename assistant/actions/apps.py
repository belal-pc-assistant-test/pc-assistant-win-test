"""/install and /uninstall through winget (Round 9).

Rules this module keeps:

* Only ids on the allow-list (``assistant/app_allowlist.py``) can run. The executor
  re-checks the name and id at run time, so a changed list cannot swap the id.
* Arguments are fixed. Install: ``--exact --id <id> --source winget
  --accept-package-agreements --accept-source-agreements``. Uninstall uses the same
  options except ``--accept-package-agreements``, which ``winget uninstall`` does not
  accept. No ``--force``, ``--ignore-security-hash``, ``--override``, custom installer
  switches, manifests, URLs or files. winget's signature and hash checks stay on.
* Nothing is installed while paused, or when winget is missing.
* Success is reported only when winget exits with code 0. Windows shows a UAC prompt on
  the PC screen for machine installs; the bot never answers it.
"""
from __future__ import annotations

import re
import shutil
import time

from .. import app_allowlist, pause, winshell
from .base import ActionContext, ActionResult, run_process

WINGET_TIMEOUT = 1800  # seconds; downloads and UAC prompts can take a long time
_PROGRESS_ONLY = re.compile(r"[\d.,\s]*(?:KB|MB|GB|%)?[\s/\d.,]*(?:KB|MB|GB|%)?\s*")


def winget_path() -> str | None:
    """Where winget.exe is, or None. Checked before any run."""
    return shutil.which("winget")


def winget_args(action: str, app_id: str) -> list[str]:
    """The fixed argument list for one action. Anything else is a programming error."""
    if action == "app_install":
        return ["install", "--exact", "--id", app_id, "--source", "winget",
                "--accept-package-agreements", "--accept-source-agreements"]
    if action == "app_uninstall":
        return ["uninstall", "--exact", "--id", app_id, "--source", "winget",
                "--accept-source-agreements"]
    raise ValueError(f"not a winget action: {action}")


def _is_progress(line: str) -> bool:
    text = line.strip()
    if not text:
        return True
    if any(mark in text for mark in "█▒░"):
        return True  # download or install bar, even with numbers after it
    if set(text) <= set("-\\|/ "):
        return True  # spinner
    return _PROGRESS_ONLY.fullmatch(text) is not None


def failure_reason(output: str) -> str:
    """Last few meaningful lines of winget output, sanitized and short."""
    lines = [line for line in (output or "").splitlines() if not _is_progress(line)]
    return winshell.sanitize_failure("\n".join(lines[-3:]), limit=200)


def _paused(config) -> bool:
    try:
        return pause.load_pause(pause.pause_path(config.jobs_file)) is not None
    except (OSError, ValueError):
        return True  # fail closed


def _run(action: str, params: dict, context: ActionContext) -> ActionResult:
    started = time.perf_counter()
    config = context.config

    def refuse(text: str) -> ActionResult:
        return ActionResult(action, False, text, duration=time.perf_counter() - started)

    if set(params) != {"name", "id"}:
        return refuse("❌ The app request is malformed; nothing was run.")
    name, app_id = params["name"], params["id"]
    allowed = app_allowlist.allowed_apps(config.extra_allowed_apps)
    entry = allowed.get(name)
    if entry is None or entry["id"] != app_id:
        return refuse(
            "❌ That app is not on the allow-list any more, so nothing was run. "
            "Request it again."
        )
    if _paused(config):
        return refuse("⏸ Paused: nothing was installed or removed. Send /resume first.")
    exe = winget_path()
    if exe is None:
        return refuse(
            "❌ winget is not available on this PC, so nothing was run. Install or update "
            "\"App Installer\" from the Microsoft Store, then try again."
        )

    title = entry["title"]
    argv = [exe, *winget_args(action, app_id)]
    proc = run_process(argv, WINGET_TIMEOUT, None, env=winshell.child_env())
    code = proc.returncode
    output = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    verb = "installed" if action == "app_install" else "removed"
    if code == 0:
        text = f"✅ winget reported success (exit code 0): {title} ({app_id}) was {verb}."
    else:
        text = (f"❌ winget did not finish {title} ({app_id}). Exit code {code} "
                f"(0x{code & 0xFFFFFFFF:08X}).")
        reason = failure_reason(output)
        if reason:
            text += f"\nReason: {reason}"
    return ActionResult(action, code == 0, text, duration=time.perf_counter() - started)


def app_install(params: dict, context: ActionContext) -> ActionResult:
    return _run("app_install", params, context)


def app_uninstall(params: dict, context: ActionContext) -> ActionResult:
    return _run("app_uninstall", params, context)
