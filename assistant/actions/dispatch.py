"""The action registry: one place that maps an action name to its handler."""

from __future__ import annotations

import time
from typing import Any, Callable

from .. import audit
from ..action_spec import ACTION_ICONS, ACTION_TITLES, RISK_ICONS, risk_of
from ..config import Config
from ..planner import Step
from . import automation, core, system, transfer, personal
from .base import (
    ActionContext,
    ActionError,
    ActionResult,
    Transport,
    abort_requested,
)

Handler = Callable[[dict, ActionContext], ActionResult]

HANDLERS: dict[str, Handler] = {
    "reply": core.reply,
    "memory_update": personal.memory_update,
    "memory_read": personal.memory_read,
    "briefing_schedule": personal.briefing_schedule,
    "briefing": personal.briefing,
    "job_cancel": personal.job_cancel,
    "jobs_read": personal.jobs_read,
    "audit_read": personal.audit_read,
    "reminder_delivery": personal.reminder_delivery,
    "cloud_plan": personal.cloud_plan,
    "watch_start": personal.watch_action,
    "watch_speed": personal.watch_action,
    "powershell": core.powershell,
    "open": core.open_target,
    "open_url": core.open_url,
    "screenshot": core.take_screenshot,
    "read_file": core.read_file,
    "write_file": core.write_file,
    "list_dir": core.list_dir,
    "system_info": system.system_info,
    "list_processes": system.list_processes,
    "kill_process": system.kill_process,
    "volume": system.volume,
    "lock_screen": system.lock_screen,
    "clipboard_get": system.clipboard_get,
    "clipboard_set": system.clipboard_set,
    "notification": system.notification,
    "window_screenshot": system.window_screenshot,
    "power": system.power,
    "reminder": automation.reminder,
    "scheduled_task": automation.scheduled_task,
    "find_files": transfer.find_files,
    "send_file": transfer.send_file,
    "receive_file": transfer.receive_file,
}


def execute_step(
    step: Step,
    config: Config,
    transport: Transport | None = None,
    jobs: Any = None,
    chat_id: int | None = None,
    capture_allowed: bool = False,
    capture_window_handle: int = 0,
) -> ActionResult:
    """Run one step and never raise: failures come back as ``ok=False``."""
    started = time.perf_counter()
    if abort_requested():
        return ActionResult(step.action, False, "❌ Aborted by /panic before starting.")

    handler = HANDLERS.get(step.action)
    if handler is None:
        return ActionResult(step.action, False, f"❌ Unsupported action: {step.action}")

    context = ActionContext(
        config=config, transport=transport, jobs=jobs, chat_id=chat_id,
        capture_allowed=capture_allowed, capture_window_handle=capture_window_handle
    )
    try:
        result = handler(step.params, context)
        if not result.duration:
            result.duration = time.perf_counter() - started
        return result
    except ActionError as exc:
        return ActionResult(
            step.action, False, f"❌ {exc}", duration=time.perf_counter() - started
        )
    except Exception as exc:  # unexpected -> report, never crash the bot
        audit.error(f"Unexpected error in action {step.action}: {exc}", exc_info=True)
        return ActionResult(
            step.action, False, f"❌ Unexpected error: {exc}",
            duration=time.perf_counter() - started,
        )


def step_preview(step: Step, index: int, detail_limit: int = 450) -> str:
    """One readable block describing a step, with its risk label."""
    icon = ACTION_ICONS.get(step.action, "•")
    title = ACTION_TITLES.get(step.action, step.action)
    risk = risk_of(step.action)
    detail = step.target_text()
    if len(detail) > detail_limit:
        detail = detail[:detail_limit] + " …"

    lines = [f"{index}. {icon} {title}  {RISK_ICONS[risk]} {risk}"]
    if detail:
        lines.append(f"    {detail}")
    if step.description and step.description != detail:
        lines.append(f"    {step.description}")
    if step.warning:
        lines.append(f"    {step.warning}")
    return "\n".join(lines)
