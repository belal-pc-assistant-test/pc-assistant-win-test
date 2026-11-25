"""Confirmed note, macro, and briefing actions."""
from __future__ import annotations
from datetime import datetime
from .base import ActionContext, ActionError, ActionResult
from . import system
from ..memory import MemoryStore


def _store(ctx):
    if ctx.chat_id is None:
        raise ActionError('This action requires a private Telegram chat.')
    return MemoryStore(ctx.config.jobs_file.parent / 'memory.json')


def memory_update(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    try:
        message = _store(ctx).mutate(ctx.chat_id, params.get('op', ''), params.get('value', ''), params.get('name', ''), params.get('steps', ''))
    except (ValueError, OSError) as exc:
        raise ActionError(str(exc)) from None
    return ActionResult('memory_update', True, message)


def memory_read(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    try:
        user = _store(ctx).get(ctx.chat_id)
    except ValueError as exc:
        raise ActionError(str(exc)) from None
    if params.get('kind') == 'macros':
        text = '\n'.join(sorted(user.get('macros', {}).keys())) or '(no macros)'
        return ActionResult('memory_read', True, 'Macros:\n' + text[:ctx.config.max_output_chars])
    notes = user.get('notes', [])
    text = '\n'.join(f'{i}. {note}' for i, note in enumerate(notes, 1)) or '(no notes)'
    return ActionResult('memory_read', True, 'Notes:\n' + text[:ctx.config.max_output_chars])


def briefing_schedule(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    if ctx.jobs is None or ctx.chat_id is None:
        raise ActionError('Briefings need the bot job store and a private chat.')
    try:
        job = ctx.jobs.add_briefing(ctx.chat_id, params.get('when', ''))
    except ValueError as exc:
        raise ActionError(str(exc)) from None
    return ActionResult('briefing_schedule', True, f'Daily briefing {job.id} scheduled at {job.due_dt:%H:%M}. Each delivery needs approval; remove with /canceltask {job.id}.')


def briefing(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    if ctx.chat_id is None or ctx.jobs is None:
        raise ActionError('Briefing requires a chat and job store.')
    try:
        user = _store(ctx).get(ctx.chat_id)
    except ValueError as exc:
        raise ActionError(str(exc)) from None
    report = system.system_info({'sections': 'battery,disks'}, ctx).output
    if '🔋' not in report:
        report += '\nBattery: unavailable or no battery detected.'
    if '🗄️' not in report:
        report += '\nDisk space: unavailable.'
    reminders = [j for j in ctx.jobs.all() if j.kind == 'reminder' and j.chat_id == ctx.chat_id]
    notes = user.get('notes', [])
    parts = [f'Daily briefing — {datetime.now():%Y-%m-%d %H:%M}', report,
             'Pending reminders: ' + str(len(reminders))]
    parts.extend(f'• {j.due_dt:%Y-%m-%d %H:%M}: {j.text[:200]}' for j in reminders[:10])
    parts.append('Notes:')
    parts.extend(f'• {str(note)[:300]}' for note in notes[:10])
    if not notes:
        parts.append('(none)')
    return ActionResult('briefing', True, '\n'.join(parts)[:ctx.config.max_output_chars])


def job_cancel(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    if ctx.jobs is None or ctx.chat_id is None:
        raise ActionError('Job store is unavailable.')
    job = ctx.jobs.get(params.get('id', ''))
    if job is None or job.chat_id != ctx.chat_id:
        raise ActionError('No matching job in this chat.')
    ctx.jobs.remove(job.id)
    return ActionResult('job_cancel', True, f'Deleted job {job.id}.')


def jobs_read(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    if ctx.jobs is None or ctx.chat_id is None:
        raise ActionError('Job store is unavailable.')
    jobs = [j for j in ctx.jobs.all() if j.chat_id == ctx.chat_id]
    text = '\n'.join(j.describe() for j in jobs) or '(no jobs)'
    return ActionResult('jobs_read', True, 'Scheduled jobs:\n' + text[:ctx.config.max_output_chars])


def audit_read(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    from .. import audit
    return ActionResult('audit_read', True, audit.tail(ctx.config.log_file, 40)[:ctx.config.max_output_chars])


def reminder_delivery(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    text = params.get('text', '').strip()
    if not text:
        raise ActionError('Reminder text is empty.')
    return ActionResult('reminder_delivery', True, 'Reminder: ' + text)


def cloud_plan(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    raise ActionError('Cloud planning requires the Telegram confirmation flow.')


def watch_action(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    raise ActionError('Screen sharing requires the Telegram approval and indicator flow.')
