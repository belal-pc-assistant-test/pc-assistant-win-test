"""Reminders and scheduled commands, persisted to a local JSON file.

Nothing here hides itself or registers anything with Windows: jobs live in
``[paths] jobs_file`` (``data/jobs.json`` by default), are executed by the
assistant while it runs, and disappear when you delete them.

Due jobs are queued for an additional explicit human approval before execution
or delivery; no scheduled command runs directly from the job file.
"""

from __future__ import annotations

import json
import os
import re
import threading
from functools import wraps
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Iterable

from . import audit
from .plan_code import generate_code, unique_code

KIND_REMINDER = "reminder"
KIND_TASK = "task"
KIND_BRIEFING = "briefing"
REPEATS = {"once", "daily"}


# ---------------------------------------------------------------------------
# "when" parsing
# ---------------------------------------------------------------------------

RELATIVE_RE = re.compile(r"^\+?\s*(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hour|hours|d|day|days)$", re.I)
TIME_ONLY_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
FULL_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})$")
DATE_ONLY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")

ARABIC_UNITS = {"ث": "s", "ثانية": "s", "ثواني": "s", "د": "m", "دقيقة": "m",
                "دقائق": "m", "س": "h", "ساعة": "h", "ساعات": "h", "ي": "d", "يوم": "d", "أيام": "d"}


def parse_when(raw: str, now: datetime | None = None) -> datetime:
    """Parse ``+10m`` / ``10m`` / ``18:30`` / ``2026-01-31 18:30`` into a datetime."""
    text = (raw or "").strip()
    if not text:
        raise ValueError(
            "Give a time: +10m, +2h, +1d, 18:30, or 2026-01-31 18:30."
        )
    now = now or datetime.now()

    # Arabic relative form: "بعد 10 دقائق"
    arabic = re.sub(r"^(بعد|خلال)\s*", "", text)
    match = re.match(r"^(\d+)\s*([^\d\s]+)$", arabic)
    if match and match.group(2) in ARABIC_UNITS:
        text = f"{match.group(1)}{ARABIC_UNITS[match.group(2)]}"

    match = RELATIVE_RE.match(text)
    if match:
        amount = int(match.group(1))
        unit = match.group(2).lower()[0]
        delta = {
            "s": timedelta(seconds=amount),
            "m": timedelta(minutes=amount),
            "h": timedelta(hours=amount),
            "d": timedelta(days=amount),
        }[unit]
        return now + delta

    match = TIME_ONLY_RE.match(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("That time of day does not exist.")
        candidate = datetime.combine(now.date(), dtime(hour, minute))
        if candidate <= now:
            candidate += timedelta(days=1)   # already passed today -> tomorrow
        return candidate

    match = FULL_RE.match(text)
    if match:
        year, month, day, hour, minute = (int(g) for g in match.groups())
        try:
            return datetime(year, month, day, hour, minute)
        except ValueError as exc:
            raise ValueError(f"Invalid date/time: {exc}") from None

    match = DATE_ONLY_RE.match(text)
    if match:
        year, month, day = (int(g) for g in match.groups())
        try:
            return datetime.combine(datetime(year, month, day).date(), dtime(9, 0))
        except ValueError as exc:
            raise ValueError(f"Invalid date: {exc}") from None

    raise ValueError(
        f"Could not understand the time {raw!r}. Use +10m, +2h, +1d, 18:30 or "
        "2026-01-31 18:30."
    )


def format_when(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# Job model + store
# ---------------------------------------------------------------------------

@dataclass
class Job:
    id: str
    kind: str                 # "reminder" | "task"
    due: str                  # ISO local time "YYYY-MM-DDTHH:MM:SS"
    chat_id: int
    created: str = ""
    text: str = ""            # reminder text
    command: str = ""         # task: PowerShell command
    name: str = ""            # task: optional label
    repeat: str = "once"      # "once" | "daily"

    @property
    def due_dt(self) -> datetime:
        return datetime.fromisoformat(self.due)

    def describe(self) -> str:
        what = self.text if self.kind == KIND_REMINDER else (self.name or self.command)
        stamp = self.due_dt.strftime("%Y-%m-%d %H:%M")
        repeat = "" if self.repeat == "once" else " (daily)"
        return f"{self.id} · {stamp}{repeat} · {what[:120]}"


def _synchronized(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return guarded


@dataclass
class JobStore:
    """Tiny JSON-backed job list (survives restarts)."""

    path: Path
    jobs: list[Job] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.reload()

    # -- persistence ----------------------------------------------------
    @_synchronized
    def reload(self) -> list[Job]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            self.jobs = []
            return self.jobs
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            audit.warning("jobs file is corrupt, starting empty", path=str(self.path))
            self.jobs = []
            return self.jobs
        jobs: list[Job] = []
        for item in data.get("jobs", []) if isinstance(data, dict) else []:
            try:
                jobs.append(Job(**item))
            except TypeError:
                continue
        self.jobs = jobs
        return self.jobs

    @_synchronized
    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "jobs": [asdict(job) for job in self.jobs]}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    # -- queries --------------------------------------------------------
    @_synchronized
    def all(self) -> list[Job]:
        return sorted(self.jobs, key=lambda job: job.due)

    @_synchronized
    def get(self, job_id: str) -> Job | None:
        wanted = (job_id or "").strip().upper()
        for job in self.jobs:
            if job.id.upper() == wanted:
                return job
        return None

    @_synchronized
    def due(self, now: datetime | None = None) -> list[Job]:
        now = now or datetime.now()
        return [job for job in self.all() if job.due_dt <= now]

    # -- mutations ------------------------------------------------------
    def _new_id(self) -> str:
        return unique_code([job.id for job in self.jobs])

    @_synchronized
    def add_reminder(self, chat_id: int, text: str, when: str) -> Job:
        due = parse_when(when)
        job = Job(
            id=self._new_id(), kind=KIND_REMINDER, due=due.isoformat(timespec="seconds"),
            chat_id=chat_id, created=datetime.now().isoformat(timespec="seconds"),
            text=text.strip(),
        )
        self.jobs.append(job)
        self.save()
        audit.event("job_added", job_kind=job.kind, id=job.id, due=job.due)
        return job

    @_synchronized
    def add_briefing(self, chat_id: int, when: str) -> Job:
        if not TIME_ONLY_RE.fullmatch(when.strip()):
            raise ValueError('Use a daily local time in HH:MM format.')
        due = parse_when(when)
        job = Job(id=self._new_id(), kind=KIND_BRIEFING, due=due.isoformat(timespec="seconds"),
                  chat_id=chat_id, created=datetime.now().isoformat(timespec="seconds"),
                  name='Daily briefing (requires approval)', repeat='daily')
        self.jobs.append(job)
        self.save()
        audit.event('job_added', job_kind=job.kind, id=job.id)
        return job

    @_synchronized
    def add_task(self, chat_id: int, command: str, when: str,
                 repeat: str = "once", name: str = "") -> Job:
        due = parse_when(when)
        repeat = (repeat or "once").strip().lower()
        if repeat not in REPEATS:
            raise ValueError(f"repeat must be one of {sorted(REPEATS)}")
        job = Job(
            id=self._new_id(), kind=KIND_TASK, due=due.isoformat(timespec="seconds"),
            chat_id=chat_id, created=datetime.now().isoformat(timespec="seconds"),
            command=command.strip(), name=name.strip(), repeat=repeat,
        )
        self.jobs.append(job)
        self.save()
        audit.event("job_added", job_kind=job.kind, id=job.id, due=job.due, repeat=repeat)
        return job

    @_synchronized
    def remove(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        self.jobs.remove(job)
        self.save()
        audit.event("job_removed", id=job.id)
        return True

    @_synchronized
    def finish(self, job: Job) -> None:
        """Remove a one-shot job, or push a daily job to the next day."""
        if job.repeat == "daily":
            next_due = job.due_dt + timedelta(days=1)
            while next_due <= datetime.now():
                next_due += timedelta(days=1)
            job.due = next_due.isoformat(timespec="seconds")
        else:
            if job in self.jobs:
                self.jobs.remove(job)
        self.save()


def new_job_id(existing: Iterable[str] = ()) -> str:
    return unique_code(existing) if existing else generate_code()
