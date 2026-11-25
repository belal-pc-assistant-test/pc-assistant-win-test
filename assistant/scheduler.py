"""Queue due jobs for explicit confirmation; never execute them here."""
from __future__ import annotations
import asyncio
from typing import Any
from . import audit
from .actions.base import abort_requested
from .config import Config
from .jobs import JobStore, KIND_REMINDER, KIND_TASK, KIND_BRIEFING

TICK_SECONDS = 15


async def process_due_jobs(store: JobStore, bot: Any, config: Config, queue_job=None) -> int:
    if abort_requested() or queue_job is None:
        return 0
    processed = 0
    for job in store.due():
        if abort_requested():
            break
        if not config.is_allowed(job.chat_id) or job.kind not in {KIND_REMINDER, KIND_TASK, KIND_BRIEFING}:
            continue
        try:
            queued = await queue_job(job)
        except Exception:
            audit.warning('Unable to queue due job')
            continue
        if queued:
            store.finish(job)
            processed += 1
    return processed


async def scheduler_loop(store: JobStore, bot: Any, config: Config, queue_job=None) -> None:
    audit.event('scheduler_started', jobs=len(store.jobs))
    while True:
        try:
            store.reload()
            await process_due_jobs(store, bot, config, queue_job)
        except asyncio.CancelledError:
            raise
        except Exception:
            audit.warning('Scheduler tick failed')
        await asyncio.sleep(TICK_SECONDS)
