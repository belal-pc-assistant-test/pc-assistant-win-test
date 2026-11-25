"""Automation actions: reminders and scheduled commands (persisted jobs)."""

from __future__ import annotations

import time

from .base import ActionContext, ActionError, ActionResult


def _store(ctx: ActionContext):
    if ctx.jobs is None:
        raise ActionError(
            "Reminders and scheduled tasks need the bot's job store; this action "
            "can only run from the Telegram bot."
        )
    return ctx.jobs


def reminder(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    started = time.perf_counter()
    text = (params.get("text") or "").strip()
    when = (params.get("when") or "").strip()
    if not text:
        raise ActionError("A reminder needs some text.")
    if ctx.chat_id is None:
        raise ActionError("A reminder needs a chat to be delivered to.")

    try:
        job = _store(ctx).add_reminder(ctx.chat_id, text, when)
    except ValueError as exc:
        raise ActionError(str(exc)) from None

    return ActionResult(
        "reminder", True,
        f"⏰ Reminder {job.id} set for {job.due_dt.strftime('%Y-%m-%d %H:%M')} "
        f"({job.text[:120]}). Saved to disk; delivery will require a new confirmation.",
        duration=time.perf_counter() - started,
    )


def scheduled_task(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    started = time.perf_counter()
    command = (params.get("command") or "").strip()
    when = (params.get("when") or "").strip()
    repeat = (params.get("repeat") or "once").strip().lower()
    name = (params.get("name") or "").strip()
    if not command:
        raise ActionError("A scheduled task needs a PowerShell command.")
    if ctx.chat_id is None:
        raise ActionError("A scheduled task needs a chat to report back to.")

    try:
        job = _store(ctx).add_task(ctx.chat_id, command, when, repeat=repeat, name=name)
    except ValueError as exc:
        raise ActionError(str(exc)) from None

    return ActionResult(
        "scheduled_task", True,
        f"🕒 Task {job.id} scheduled for "
        f"{job.due_dt.strftime('%Y-%m-%d %H:%M')} ({job.repeat}).\n"
        f"Command: <pre>{command[:300]}</pre>\n"
        f"Saved to disk; each execution requires a new confirmation. Remove it with /canceltask {job.id}.",
        duration=time.perf_counter() - started,
    )
