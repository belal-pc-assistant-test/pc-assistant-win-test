"""Telegram bot layer: authorisation, plan codes, confirmation UI, execution.

Security model in one paragraph: only ids in ``allowed_user_ids`` may ask for
plans, only ids in ``approver_user_ids`` may approve them, every plan must be
confirmed (inline button or ``approve CODE``), a code is single use and bound to
the exact steps by a fingerprint, and every step result is audited. ``/panic``
cancels everything pending and kills running child processes.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import html
import json
from collections import deque
import platform
import sys
from urllib.parse import urlparse
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from . import audit
from . import app_allowlist, health
from .action_spec import (
    ACTION_ICONS,
    ACTION_TITLES,
    RISK_EXPLANATIONS,
    RISK_ICONS,
    VALID_ACTIONS,
    risk_of,
)
from .actions import execute_step, request_abort, clear_abort, abort_requested
from .actions.apps import winget_path
from .actions.base import REGISTRY, sanitize_filename, unique_destination, ActionResult, os_error_reason
from .config import Config
from .jobs import JobStore
from .planner import Plan, Planner, Step
from .plan_code import fingerprint, normalize_code, unique_code
from .scheduler import scheduler_loop
from .memory import MemoryStore, language_for, NAME_RE
from .outbound import RedactingHTTPXRequest
from .pause import is_exempt as pause_exempt, load_pause, pause_path, save_pause
from .voice import transcribe, VoiceUnavailable, MAX_VOICE_BYTES, MAX_VOICE_SECONDS
from .sharing import (ScreenIndicator, WatchController, CaptureLease, capture_frame, photo_stream, foreground_window_handle,
                      MIN_INTERVAL, MAX_INTERVAL, DEFAULT_INTERVAL, DEFAULT_MINUTES, MAX_MINUTES)
from .planner import build_plan, PlannerError
from .remote.session import RemoteController
from .remote.runtime import make_deps as make_remote_deps

MAX_TG_LEN = 4000
PLAN_TTL_SECONDS = 15 * 60      # a pending plan expires after 15 minutes
MAX_PENDING_PLANS = 50          # hard cap on approvals waiting at once (memory bound)
MAX_REQUEST_CHARS = 2000        # longer text is refused before any planner sees it
FLOOD_LIMIT = 30                # updates per user accepted within FLOOD_WINDOW
FLOOD_WINDOW = 10.0             # seconds
UNAUTHORISED_LOG_INTERVAL = 60.0  # at most one log line per stranger per minute
CODE_RE = re.compile(r"^(approve|deny|موافق|رفض|أرفض)\s+([A-Za-z0-9]{4,12})$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

MSG = {
    "start": (
        "🤖 <b>PC Assistant is online</b>\n\n"
        "I turn your messages into actions on this PC — but nothing runs until "
        "an approver presses <b>✅ Yes</b> or sends <code>approve CODE</code>.\n\n"
        "Send me a normal sentence in English or Arabic, for example:\n"
        "• <code>take a screenshot</code> / <code>خد لقطة شاشة</code>\n"
        "• <code>how is my battery?</code> / <code>اعرض الملفات في C:\\Users</code>\n"
        "• <code>find pdf files in C:\\Users\\Me\\Documents</code>\n"
        "• <code>remind me to call mom in 30 minutes</code>\n"
        "• <code>!Get-Process</code> — anything starting with <code>!</code> runs "
        "as a raw PowerShell command.\n\n"
        "Every plan comes with a 6-character code. Approve with the buttons or "
        "<code>approve CODE</code>; refuse with <code>deny CODE</code>.\n\n"
        "/help for everything · /status diagnostics · /privacy where your text goes · "
        "/panic stops everything · /pause holds new work until /resume"
    ),
    "help": (
        "📖 <b>Commands</b>\n"
        "/start – intro\n"
        "/help – this text\n"
        "/id (or /whoami) – show your Telegram user id\n"
        "/status – planner, approvers, jobs, config\n"
        "/privacy – where your request text goes\n"
        "/jobs – list reminders and scheduled tasks\n"
        "/canceltask CODE – delete a reminder/task\n"
        "/cancel – drop my pending plans\n"
        "/yes CODE /no CODE – approve or deny; /approve and /deny are aliases\n"
        "/panic – cancel ALL pending plans and abort running ones\n"
        "/pause – stop all new work until /resume (survives a restart); /status and /panic still work\n"
        "/resume – continue after /pause\n"
        "/logs – last lines of the local audit log (approval required)\n"
        "/screen – full-screen photo; /window [title] – window photo (approval required)\n"
        "/watch start [seconds] [minutes] – visible screen sharing; /watch speed N – change speed with approval\n"
        "/watch stop – stop immediately; default 3s/10min, min 2s, max 60min\n"
        "/remote start [minutes] – private remote desktop: full view AND mouse/keyboard control; one approval; default 15, max 60 min\n"
        "/remote status – session state (never the link) · /remote stop – end now and close the tunnel\n"
        "/health – read-only PC check: disk, memory, uptime, restart pending, Defender, firewall, Windows Update (no approval)\n"
        "/install NAME – install an allow-listed app with winget (approval needed, HIGH RISK, UAC prompt on the PC)\n"
        "/uninstall NAME – remove an allow-listed app with winget (approval needed, HIGH RISK)\n"
        "A visible indicator is required; photos are sent only to allowed private chats.\n"
        "/note TEXT – save a note; /notes – view notes; /forget N|language|all – forget memory\n"
        "/language en|ar – save a language preference\n"
        "/briefing now – request a briefing; /briefing daily HH:MM – schedule daily approval requests\n"
        "/chain STEP ; STEP – propose a multi-step offline plan\n"
        "/macro save NAME – save a pending plan; /macro run NAME – run a saved macro with approval\n"
        "/macro delete NAME; /macros – list macros\n"
        "Voice notes: optional local Whisper; offline keyword planning. Approve actions with buttons or text.\n"
        "Scheduled reminders, commands and daily briefings each require a new approval when due.\n\n"
        "<b>Approving</b>\n"
        "Every plan gets a code (e.g. <code>K7M2QP</code>) and a fingerprint of its "
        "exact steps. Approve with the inline buttons or by typing "
        "<code>approve K7M2QP</code>; refuse with <code>deny K7M2QP</code>.\n"
        "• A code works once, only for the plan it was issued for, and expires "
        "after 15 minutes.\n"
        "• Re-planning (sending the request again) creates a new code and voids the old one.\n"
        "• Only allowed IDs can use the bot; only configured approvers can say Yes.\n\n"
        "<b>Actions</b> (all confirmed first)\n"
        "system report · list/kill processes · open app/file/URL · full &amp; window "
        "screenshots · lock · notification · volume · clipboard read/write · read, "
        "write, list, search files · send file to you · save a file you upload · "
        "reminders · scheduled commands · PowerShell · power off/restart/sleep · winget install/remove of allow-listed apps.\n"
        "Each step is labelled 🟢 low, 🟡 medium or 🔴 high risk."
    ),
    "denied": "🚫 Not authorised. This bot only serves its configured owner.\nغير مصرح لك باستخدام هذا البوت.",
    "thinking": "🧠 Planning… / جاري التخطيط…",
    "cancel_none": "Nothing is waiting for confirmation. / لا يوجد طلب معلّق.",
    "cancelled": "❌ Cancelled. Nothing was executed. / تم الإلغاء، لم يتم تنفيذ أي شيء.",
    "expired": "⌛ That plan expired or was already used. Send the request again.",
    "not_yours": "🚫 Only a configured approver may confirm plan {code}.",
    "not_approver": (
        "🚫 Your account may request plans but not approve them "
        "(see <code>approver_user_ids</code> in config.ini)."
    ),
}


# ---------------------------------------------------------------------------
# Pending plans
# ---------------------------------------------------------------------------

@dataclass
class PendingPlan:
    plan_id: str
    code: str
    fingerprint: str
    requester_id: int
    chat_id: int
    plan: Plan
    created_at: float
    watch_epoch: int = 0
    remote_epoch: int = 0
    message_refs: list[tuple[int, int]] = field(default_factory=list)


def _prune(store: dict[str, PendingPlan]) -> None:
    now = time.time()
    for key in [k for k, v in store.items() if now - v.created_at > PLAN_TTL_SECONDS]:
        store.pop(key, None)


def find_by_code(store: dict[str, PendingPlan], code: str) -> PendingPlan | None:
    wanted = normalize_code(code)
    if not wanted:
        return None
    for pending in store.values():
        if pending.code == wanted:
            return pending
    return None


def parse_code_command(text: str) -> tuple[str, str] | None:
    """``"approve K7M2QP"`` → ``("approve", "K7M2QP")``; anything else → None."""
    match = CODE_RE.match((text or "").strip())
    if not match:
        return None
    verb = match.group(1).lower()
    verb = "deny" if verb in {"deny", "رفض", "أرفض"} else "approve"
    return verb, normalize_code(match.group(2))


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + " …"


def risk_badge(risk: str) -> str:
    return f"{RISK_ICONS.get(risk, '🟡')} {risk}"


_FRIENDLY = {
    "system_info": ("Read the requested system information and send it here.", "سيتم قراءة معلومات الجهاز المطلوبة وإرسالها هنا."),
    "list_processes": ("List running processes and send the results here.", "سيتم عرض العمليات الجارية وإرسال النتائج هنا."),
    "lock_screen": ("Lock the current Windows session.", "سيتم قفل جلسة ويندوز الحالية."),
    "notification": ("Show the specified desktop notification.", "سيتم عرض التنبيه المحدد على سطح المكتب."),
    "volume": ("Change volume using the specified media keys.", "سيتم تغيير مستوى الصوت وفق المعايير المعروضة."),
    "clipboard_get": ("Read clipboard text and send it here; it may contain sensitive data.", "سيتم إرسال نص الحافظة وقد يحتوي على معلومات حساسة."),
    "clipboard_set": ("Replace the clipboard with the displayed text.", "سيتم استبدال محتوى الحافظة بالنص المعروض."),
    "read_file": ("Read the specified file and send its contents here.", "سيتم قراءة الملف المحدد وإرسال محتواه هنا."),
    "list_dir": ("List the specified folder and send the result here.", "سيتم عرض محتويات المجلد وإرسال النتيجة هنا."),
    "find_files": ("Search within allowed folders and send matching names here.", "سيتم البحث داخل المجلدات المسموحة وإرسال أسماء النتائج هنا."),
    "receive_file": ("Save the uploaded Telegram file to the displayed path.", "سيتم حفظ الملف المرفوع في المسار المعروض."),
    "reminder": ("Save a reminder; delivery requires another approval when due.", "سيتم حفظ تذكير ويتطلب إرساله موافقة جديدة عند حلول الموعد."),
    "memory_update": ("Save or change local notes, preferences, or a macro.", "سيتم تعديل الملاحظات أو التفضيلات أو الروتين المحفوظ محلياً."),
    "memory_read": ("Send your saved notes to this chat.", "سيتم إرسال ملاحظاتك إلى هذه المحادثة."),
    "briefing": ("Read PC status, reminders and notes and send them here.", "سيتم قراءة حالة الجهاز والتذكيرات والملاحظات وإرسالها هنا."),
    "briefing_schedule": ("Schedule daily requests for approval; no briefing is sent automatically.", "سيتم جدولة طلب موافقتك يومياً دون إرسال التقرير تلقائياً."),
    "powershell": ("Run this command with your Windows account's permissions.", "سيتم تشغيل الأمر بصلاحيات حساب ويندوز الخاص بك."),
    "scheduled_task": ("Schedule a command; every execution will require a new approval.", "سيتم جدولة أمر ويتطلب كل تشغيل موافقة جديدة."),
    "cloud_plan": ("Send the displayed request and context to the external planning service; you will review a second plan afterward.", "سيتم إرسال الطلب والسياق المعروضين للخدمة الخارجية، ثم ستراجع خطة ثانية."),
    "write_file": ("Write the displayed content to the file; it may overwrite it.", "سيتم كتابة المحتوى المعروض وقد يُستبدل الملف."),
    "send_file": ("Send the specified file to Telegram.", "سيتم إرسال الملف المحدد إلى تيليجرام."),
    "screenshot": ("Capture the full screen and send the photo with a visible indicator.", "سيتم التقاط الشاشة وإرسال الصورة إلى تيليجرام مع إظهار مؤشر واضح."),
    "window_screenshot": ("Capture the requested window only; no full-screen fallback. A visible indicator is required.", "سيتم تصوير النافذة فقط مع إظهار مؤشر واضح، دون التقاط الشاشة كاملة عند الفشل."),
    "watch_start": ("Start visible, temporary full-screen sharing to this private chat until stopped or timed out.", "ستبدأ مشاركة الشاشة كاملة بشكل واضح ومؤقت إلى هذه المحادثة حتى الإيقاف أو انتهاء المدة."),
    "app_install": ("Install the named app with winget: exact winget ID, winget source only, latest version. Windows will show a UAC prompt on this PC's screen; you must accept it there. The bot never answers or bypasses UAC.", "سيتم تثبيت التطبيق المحدد عبر winget: معرّف الحزمة الدقيق ومصدر winget فقط وأحدث إصدار. سيظهر مربع UAC على شاشة هذا الجهاز ويجب قبوله هناك. لا يتجاوز البوت UAC."),
    "app_uninstall": ("Remove the named app with winget (exact winget ID, winget source only). Windows will show a UAC prompt on this PC's screen; you must accept it there.", "سيتم إزالة التطبيق المحدد عبر winget (معرّف دقيق ومصدر winget فقط). سيظهر مربع UAC على شاشة هذا الجهاز ويجب قبوله هناك."),
    "remote_start": ("Give full view and mouse/keyboard control of this PC through a private link until the time ends or you stop it.", "سيتم منحك رؤية كاملة وتحكماً بالفأرة ولوحة المفاتيح في هذا الجهاز عبر رابط خاص حتى انتهاء المدة أو إيقافها."),
    "watch_speed": ("Change the interval of the existing visible sharing session.", "سيتم تغيير مدة التحديث لجلسة المشاركة الحالية المرئية."),
    "open": ("Open the specified target on the PC.", "سيتم فتح العنصر المحدد على الجهاز."),
    "open_url": ("Open the URL in a browser.", "سيتم فتح الرابط في المتصفح."),
    "power": ("Change the PC power state; data loss is possible.", "سيتم تغيير حالة طاقة الجهاز وقد تُفقد بيانات غير محفوظة."),
    "kill_process": ("Stop the specified process; unsaved work may be lost.", "سيتم إيقاف العملية المحددة وقد يُفقد العمل غير المحفوظ."),
    "job_cancel": ("Remove a scheduled job.", "سيتم حذف المهمة المجدولة."),
    "jobs_read": ("Send scheduled jobs to this chat.", "سيتم إرسال مهامك المجدولة إلى هذه المحادثة."),
    "audit_read": ("Send recent audit metadata to this chat.", "سيتم إرسال سجل الأحداث إلى هذه المحادثة."),
    "reminder_delivery": ("Send this reminder to your chat.", "سيتم إرسال التذكير إلى محادثتك."),
}

def friendly_step(action: str, language: str) -> str:
    pair = _FRIENDLY.get(action, (f"Perform the {ACTION_TITLES.get(action, action)} action using the parameters below.",
                                  f"سيتم تنفيذ الإجراء {ACTION_TITLES.get(action, action)} بالمعايير أدناه."))
    return pair[1 if language == 'ar' else 0]


def render_plan_message(
    plan: Plan,
    code: str,
    fingerprint_value: str,
    requester: str = "",
    approvers: list[int] | None = None,
    detail_limit: int = 450,
) -> str:
    lang = getattr(plan, "language", "en")
    risk_explanation = {
        "low": "قد يكشف معلومات أو يفتح برامج؛ راجع التفاصيل.",
        "medium": "يغيّر بيانات محلية أو يشارك معلومات؛ راجع التفاصيل.",
        "high": "قد يسبب الأمر أو الإجراء فقدان بيانات؛ راجعه بعناية.",
    }
    if lang == "ar":
        parts = [f"🧠 <b>الخطة المقترحة</b> <i>({_esc(plan.label())})</i>",
                 f"الخطر: {risk_badge(plan.risk)} — {_esc(risk_explanation.get(plan.risk, ''))}", "",
                 _esc(_clip(plan.summary, 400)), ""]
    else:
        parts = [f"🧠 <b>Planned</b> <i>({_esc(plan.label())})</i>",
                 f"Risk: {risk_badge(plan.risk)} — {_esc(RISK_EXPLANATIONS.get(plan.risk, ''))} · Review the details before approving.", "",
                 _esc(_clip(plan.summary, 400)), ""]
    if getattr(plan, "transcript", ""):
        parts.append("🎙️ " + ("النص المتعرف عليه:" if getattr(plan, "language", "en") == "ar" else "Recognized voice:"))
        parts.append(f"<pre>{_esc(plan.transcript)}</pre>")
        parts.append("")
    if requester:
        parts.append(f"👤 Requested by {_esc(requester)}")
        if approvers:
            parts.append(
                "🔐 Approval required from: "
                + _esc(", ".join(str(a) for a in approvers))
            )
        parts.append("")

    lang = getattr(plan, "language", "en")
    for index, step in enumerate(plan.steps, start=1):
        icon = ACTION_ICONS.get(step.action, "•")
        title = ACTION_TITLES.get(step.action, step.action)
        # Show every exact parameter, not a truncated summary, before approval.
        detail = json.dumps(step.params, ensure_ascii=False, sort_keys=True)
        parts.append(
            f"<b>{index}. {icon} {_esc(title)}</b>  {risk_badge(_risk(step.action))}"
        )
        parts.append(_esc(friendly_step(step.action, lang)))
        if detail:
            parts.append(f"<pre>{_esc(detail)}</pre>")
        if step.description and step.description != detail:
            parts.append(f"<i>{_esc(step.description)}</i>")
        if step.warning:
            parts.append(f"<i>{_esc(step.warning)}</i>")
        parts.append("")

    if plan.note:
        parts.append(f"<i>ℹ️ {_esc(_clip(plan.note, 300))}</i>")
        parts.append("")

    if lang == 'ar':
        parts.extend([
            f"🔑 الرمز: <code>{_esc(code)}</code> · البصمة <code>{_esc(fingerprint_value)}</code>",
            f"للموافقة اضغط ✅ أو أرسل <code>موافق {_esc(code)}</code> · للرفض اضغط ❌ أو أرسل <code>رفض {_esc(code)}</code>",
            "⚡ <b>لم يتم تنفيذ أي شيء بعد.</b>",
        ])
    else:
        parts.extend([
            f"🔑 Code: <code>{_esc(code)}</code> · fingerprint <code>{_esc(fingerprint_value)}</code>",
            f"Approve: press ✅ or send <code>approve {_esc(code)}</code> · refuse: ❌ or <code>deny {_esc(code)}</code>",
            "⚡ <b>Nothing has run yet.</b> / <b>لم يتم تنفيذ أي شيء بعد.</b>",
        ])
    return "\n".join(parts)


def _risk(action: str) -> str:
    return risk_of(action)


def render_plan_plain(plan: Plan) -> str:
    lines = [f"summary={plan.summary}", f"planner={plan.planner} model={plan.model}",
             f"risk={plan.risk}"]
    for index, step in enumerate(plan.steps, start=1):
        line = f"  {index}. {step.action} :: {step.target_text()}"
        if step.warning:
            line += f"  [{step.warning}]"
        lines.append(line)
    return "\n".join(lines)


def loggable_params(params: dict[str, Any]) -> dict[str, str]:
    """Keep the audit log readable: cap very long values (e.g. file content)."""
    out: dict[str, str] = {}
    for key, value in params.items():
        text = str(value)
        out[key] = text if len(text) <= 500 else f"{text[:500]}… [{len(text)} chars]"
    return out


def build_keyboard(plan_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Yes, run it", callback_data=f"apv:{plan_id}"),
            InlineKeyboardButton("❌ No, cancel", callback_data=f"deny:{plan_id}"),
        ]]
    )


def utf16_len(text: str) -> int:
    """Telegram counts message length in UTF-16 units, not Python characters."""
    return len(text.encode("utf-16-le")) // 2


def _utf16_prefix(text: str, limit: int) -> str:
    """The longest prefix of ``text`` whose UTF-16 length is at most ``limit``."""
    units = 0
    for index, char in enumerate(text):
        units += 2 if ord(char) > 0xFFFF else 1
        if units > limit:
            return text[:index]
    return text


def split_message(text: str, limit: int = MAX_TG_LEN) -> list[str]:
    chunks: list[str] = []
    remaining = text or ""
    while utf16_len(remaining) > limit:
        window = _utf16_prefix(remaining, limit) or remaining[:1]
        cut = window.rfind("\n")
        if cut < len(window) // 2:
            cut = len(window)
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks or [""]


async def send_long(bot: Any, chat_id: int, text: str, **kwargs: Any) -> None:
    """Send text in Telegram-sized chunks, degrading to plain text on formatting errors.

    Secrets are removed before splitting, so no chunk can carry part of one. Rate limits and
    dropped connections are retried once by the outbound layer (``assistant/outbound.py``).
    """
    cleaned = audit.redact_secret(text or "(empty)")
    for chunk in split_message(cleaned):
        await _send_chunk(bot, chat_id, chunk, **kwargs)


async def _send_chunk(bot: Any, chat_id: int, chunk: str, **kwargs: Any) -> None:
    try:
        await bot.send_message(chat_id=chat_id, text=chunk, **kwargs)
        return
    except RetryAfter:
        # Already retried once by the outbound layer. Plain text would hit the same limit.
        audit.warning("Message not sent", reason="rate limit")
        return
    except TelegramError as exc:
        audit.warning("Formatted send failed, retrying as plain text", error=str(exc))
    plain = {k: v for k, v in kwargs.items() if k != "parse_mode"}
    try:
        await bot.send_message(chat_id=chat_id, text=chunk, **plain)
    except TelegramError as exc2:
        audit.warning("Failed to send message", error=str(exc2))


# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


class FloodGate:
    """Sliding-window limit on how many updates one user may send.

    Bounds Telegram flooding: a flooding allowed user (or a compromised
    account) cannot keep the bot busy. Memory is bounded by pruning idle users.
    """

    def __init__(self, limit: int = FLOOD_LIMIT, window: float = FLOOD_WINDOW) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[int, deque] = {}

    def allow(self, user_id: int | None, now: float | None = None) -> bool:
        key = -1 if user_id is None else user_id
        now = time.monotonic() if now is None else now
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        if len(self._hits) > 1000:
            for other in [k for k, v in self._hits.items() if not v or now - v[-1] > self.window]:
                self._hits.pop(other, None)
        return True


def _flood_ok(context, user_id: int | None) -> bool:
    gate = context.application.bot_data.setdefault("flood", FloodGate())
    return gate.allow(user_id)


def _log_unauthorised(context, user_id: int | None) -> None:
    """Record a stranger's attempt once a minute, without their message text.

    Writing every attempt, with its text, would let anyone grow the log file.
    """
    seen: dict = context.application.bot_data.setdefault("unauthorised_seen", {})
    key = -1 if user_id is None else user_id
    now = time.monotonic()
    last = seen.get(key)
    if last is not None and now - last < UNAUTHORISED_LOG_INTERVAL:
        return
    if len(seen) >= 1000:
        seen.clear()
    seen[key] = now
    audit.warning("Unauthorised access attempt ignored", user_id=user_id,
                  reason="unauthorised user")


def owner_only(func: Handler) -> Handler:
    """Only ids in allowed_user_ids may use the bot at all."""

    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        config: Config = context.application.bot_data["config"]
        user = update.effective_user
        user_id = user.id if user else None
        if getattr(update.effective_chat, "type", "private") != "private" or not config.is_allowed(user_id):
            _log_unauthorised(context, user_id)
            if update.callback_query:
                try:
                    await update.callback_query.answer("Not authorised.", show_alert=False)
                except TelegramError:
                    pass
            return
        if not _flood_ok(context, user_id):
            if update.callback_query:
                try:
                    await update.callback_query.answer("Too many updates; slow down.")
                except TelegramError:
                    pass
            return
        await func(update, context)

    return wrapper


def owner_or_approver(func: Handler) -> Handler:
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        config: Config = context.application.bot_data["config"]
        user = update.effective_user
        if (getattr(update.effective_chat, "type", "private") != "private" or
                not user or not config.is_allowed(user.id)):
            _log_unauthorised(context, user.id if user else None)
            if update.callback_query:
                try:
                    await update.callback_query.answer("Not authorised.")
                except TelegramError:
                    pass
            return
        if not _flood_ok(context, user.id):
            if update.callback_query:
                try:
                    await update.callback_query.answer("Too many updates; slow down.")
                except TelegramError:
                    pass
            return
        await func(update, context)
    return wrapper


def can_approve(config: Config, user_id: int | None) -> bool:
    return config.is_approver(user_id)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@owner_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        MSG["start"], parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


@owner_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        MSG["help"], parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


@owner_only
async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    config: Config = context.application.bot_data["config"]
    await update.effective_message.reply_text(
        f"Your Telegram user id: <code>{user.id}</code>\n"
        f"Name: {_esc(user.full_name or '')}\n"
        f"Authorised ids: <code>{sorted(config.allowed_user_ids)}</code>\n"
        f"Approver ids: <code>{sorted(config.approver_user_ids)}</code>\n"
        f"Can request: {'✅' if config.is_allowed(user.id) else '❌'} · "
        f"Can approve: {'✅' if config.is_approver(user.id) else '❌'}",
        parse_mode=ParseMode.HTML,
    )


@owner_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.application.bot_data["config"]
    planner: Planner = context.application.bot_data["planner"]
    jobs: JobStore | None = context.application.bot_data.get("jobs")

    status = await asyncio.to_thread(planner.status)
    mode = status.get("mode", config.planner_mode)
    chain = " → ".join(status.get("chain", []))

    cloud_line = "disabled"
    cloud = status.get("cloud")
    if cloud:
        state = f"⏸ {_esc(cloud['detail'])}" if cloud["cooling_down"] else "🟢 ready"
        cloud_line = f"{state} · <code>{_esc(cloud['model'])}</code> @ {_esc(cloud['url'])}"

    local_line = "disabled"
    local = status.get("local")
    if local:
        icon = "🟢" if local["reachable"] and "ready" in local["detail"].lower() else "🟡"
        local_line = f"{icon} {_esc(local['detail'])}"

    token_line = {
        "env": "TELEGRAM_BOT_TOKEN environment variable",
        "store": f"encrypted store ({_esc(str(config.token_store_path))})",
        "file": "⚠️ token.txt (plain text in project folder)",
    }.get(config.token_source, "not loaded")

    jobs_count = len(jobs.all()) if jobs else 0
    pending = len(context.application.bot_data["plans"])
    lines = [
        "⚙️ <b>Status</b>",
        paused_status_line(context.application.bot_data),
        f"Planner mode: <b>{_esc(mode)}</b> (chain: {_esc(chain)})",
        f"Cloud planner: {cloud_line}",
        f"Local planner: {local_line}",
        f"Actions available: {len(VALID_ACTIONS)} · step cap: {config.max_plan_steps}",
        f"Allowed (may ask): <code>{_esc(str(sorted(config.allowed_user_ids)))}</code>",
        f"Approvers (may confirm): <code>{_esc(str(sorted(config.approver_user_ids)))}</code>"
        + ("  ⚠️ two-person rule active" if config.two_person_rule else ""),
        f"File transfers ≤ {config.max_send_file_mb} MB inside "
        f"<code>{_esc(', '.join(str(p) for p in (config.allowed_roots or [config.workspace_root])))}</code>",
        f"Reminders/tasks: {jobs_count} · pending plans: {pending}",
        f"Working folder: <code>{_esc(str(config.workspace_root))}</code>",
        f"Token source: {token_line}",
        f"Log file: <code>{_esc(str(config.log_file))}</code>",
        f"Jobs file: <code>{_esc(str(config.jobs_file))}</code>",
    ]
    if config.planner_mode == "cloud":
        lines.append("⚠️ External planning requires approval before request/context is sent (/privacy)")
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


@owner_only
async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.application.bot_data["config"]
    if config.planner_mode == "cloud":
        head = (
            "🌐 <b>Planner: cloud</b>\n"
            "An external planning request requires your approval before it is sent "
            "to the configured service. The text of your "
            "message and system context (OS, local time, home and working folder) leave this PC. "
            "The plan itself is shown to you first and never runs on its own."
        )
    elif config.planner_mode == "local":
        head = ("💻 <b>Planner: local</b>\nRequests use the configured Ollama endpoint. "
                "A non-loopback endpoint requires approval; Telegram still transports chat data.")
    else:
        head = ("🔤 <b>Planner: keywords</b>\nNo model is used at all: requests are "
                "matched locally with rules; Telegram still transports chat data.")
    body = (
        "\n\n<b>Always true</b>\n"
        "• Telegram transports messages; audit logs contain metadata only at "
        f"<code>{_esc(str(config.log_file))}</code>.\n"
        "• Voice is transcribed locally and uses offline keyword planning.\n"
        "• Uploaded files are saved only inside the configured folders.\n"
        "• Do not put secrets in requests: request text is sent to the planner.\n"
        "• Nothing installs itself, hides itself or starts with Windows.\n"
        "• Change the mode with <code>[planner] mode = cloud | local | keywords</code>."
    )
    await update.effective_message.reply_text(
        head + body, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


@owner_only
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: dict[str, PendingPlan] = context.application.bot_data["plans"]
    user_id = update.effective_user.id
    mine = [p for p in store.values() if p.requester_id == user_id]
    for pending in mine:
        store.pop(pending.plan_id, None)
        await _mark_messages(context, pending, MSG["cancelled"], with_keyboard=False)
    if mine:
        audit.event("cancel", user_id=user_id, codes=[p.code for p in mine])
        await update.effective_message.reply_text(MSG["cancelled"])
    else:
        await update.effective_message.reply_text(MSG["cancel_none"])


PAUSED_NOTICE = "⏸ Paused. Nothing runs until you send /resume. /status and /panic still work."


async def pause_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every other handler. While paused it answers the allowed user and stops the update.

    Other users are left alone here: ``owner_only`` logs and refuses them as before.
    """
    app = context.application
    if not app.bot_data.get("paused"):
        return
    config: Config = app.bot_data["config"]
    user = update.effective_user
    user_id = user.id if user else None
    if getattr(update.effective_chat, "type", "private") != "private" or not config.is_allowed(user_id):
        return
    if update.callback_query is not None:
        try:
            await update.callback_query.answer("Paused. Send /resume to continue.", show_alert=False)
        except TelegramError:
            pass
        raise ApplicationHandlerStop
    message = update.effective_message
    if message is None:
        return
    if pause_exempt(message.text):
        return  # /resume, /panic and /status keep working
    try:
        await message.reply_text(PAUSED_NOTICE)
    except TelegramError:
        pass
    raise ApplicationHandlerStop


async def _stop_background_for_pause(context: ContextTypes.DEFAULT_TYPE, reason: str) -> None:
    """Stop the screen watch, remote session and capture lease the bot started itself."""
    app = context.application
    app.bot_data["watch_epoch"] = app.bot_data.get("watch_epoch", 0) + 1
    app.bot_data["remote_epoch"] = app.bot_data.get("remote_epoch", 0) + 1
    controller = app.bot_data.get("watch_controller")
    if controller is not None:
        controller.stop_now(reason)
    remote = app.bot_data.get("remote_controller")
    if remote is not None:
        await remote.stop(reason)
    lease = app.bot_data.get("screen_capture_lease")
    if lease is not None:
        lease.stop_now()


def paused_status_line(bot_data) -> str:
    """One status line: running, or paused since when."""
    if not bot_data.get("paused"):
        return "▶️ Running"
    info = bot_data.get("paused_info") or {}
    return f"⏸ <b>PAUSED</b> since {_esc(str(info.get('since', 'unknown')))} — send /resume to continue"


@owner_only
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hold all new work until /resume. The state is saved, so it survives a restart."""
    app = context.application
    user_id = update.effective_user.id
    if app.bot_data.get("paused"):
        await update.effective_message.reply_text("⏸ Already paused. Send /resume to continue.")
        return
    info = {"paused": True, "since": datetime.now().isoformat(timespec="seconds"), "by": user_id}
    app.bot_data["paused"] = True          # in memory first: a failed save must not leave work running
    app.bot_data["paused_info"] = info
    await _stop_background_for_pause(context, "pause")
    try:
        save_pause(app.bot_data["pause_file"], True, user_id)
        saved_note = "The pause is saved and survives a restart."
    except OSError as exc:
        audit.warning("Could not save the pause file", error=str(exc))
        saved_note = f"⚠️ The pause could NOT be saved ({exc}); it will end on a restart."
    audit.event("paused", user_id=user_id)
    await update.effective_message.reply_text(
        "⏸ Paused. New requests, approvals and scheduled jobs wait until /resume. "
        "A plan that is already running is not interrupted; /panic stops it.\n" + saved_note
    )


@owner_only
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    user_id = update.effective_user.id
    if not app.bot_data.get("paused"):
        await update.effective_message.reply_text("▶️ Not paused.")
        return
    app.bot_data["paused"] = False
    app.bot_data["paused_info"] = None
    try:
        save_pause(app.bot_data["pause_file"], False, None)
        note = ""
    except OSError as exc:
        audit.warning("Could not clear the pause file", error=str(exc))
        note = f"\n⚠️ The saved pause could not be cleared ({exc}); send /resume again after a restart if it returns."
    audit.event("resumed", user_id=user_id)
    await update.effective_message.reply_text("▶️ Resumed." + note)


@owner_or_approver
async def cmd_panic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel every pending plan and abort whatever is running."""
    config: Config = context.application.bot_data["config"]
    user_id = update.effective_user.id
    if not config.is_allowed(user_id):
        return

    store: dict[str, PendingPlan] = context.application.bot_data["plans"]
    cancelled = list(store.values())
    store.clear()
    context.application.bot_data["panic_epoch"] = context.application.bot_data.get("panic_epoch", 0) + 1
    running_before = REGISTRY.running
    request_abort()  # act before network waits
    context.application.bot_data["watch_epoch"] = context.application.bot_data.get("watch_epoch", 0) + 1
    context.application.bot_data["remote_epoch"] = context.application.bot_data.get("remote_epoch", 0) + 1
    controller = context.application.bot_data.get("watch_controller")
    if controller is not None:
        controller.stop_now("panic")
    remote = context.application.bot_data.get("remote_controller")
    remote_stopped = await remote.stop("panic") if remote is not None else False
    lease = context.application.bot_data.get("screen_capture_lease")
    if lease is not None:
        lease.stop_now()
    for pending in cancelled:
        await _mark_messages(
            context, pending,
            "🛑 <b>Cancelled by /panic.</b> Nothing else will run.",
            with_keyboard=False,
        )

    audit.event("panic", user_id=user_id, cancelled=[p.code for p in cancelled],
                killed=running_before)
    await update.effective_message.reply_text(
        "🛑 <b>Panic</b>\n"
        f"• Pending plans cancelled: {len(cancelled)}\n"
        f"• Running processes killed: {running_before}\n"
        f"• Remote session stopped: {'yes' if remote_stopped else 'none was running'}\n"
        "Send a new request when you are ready.",
        parse_mode=ParseMode.HTML,
    )


@owner_only
async def cmd_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await propose_plan(update, context, Plan(summary="Send the list of scheduled jobs", steps=[Step("jobs_read", {})], planner="direct"))


@owner_only
async def cmd_canceltask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await update.effective_message.reply_text("Usage: /canceltask CODE")
        return
    await propose_plan(update, context, Plan(summary="Delete scheduled job", steps=[Step("job_cancel", {"id": args[0].upper()})], planner="direct"))


@owner_only
async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await propose_plan(update, context, Plan(summary="Send the recent audit log", steps=[Step("audit_read", {})], planner="direct"))


@owner_or_approver
async def cmd_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _code_command(update, context, "approve")


@owner_or_approver
async def cmd_no(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _code_command(update, context, "deny")


async def _code_command(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        verb: str) -> None:
    args = context.args or []
    if not args:
        await update.effective_message.reply_text(
            f"Usage: <code>/{'yes' if verb == 'approve' else 'no'} CODE</code> "
            "(the code is in the plan message)",
            parse_mode=ParseMode.HTML,
        )
        return
    await _handle_code(update, context, verb, normalize_code(args[0]), via="command")


# ---------------------------------------------------------------------------
# Main text handler: plan, or approve/deny by code
# ---------------------------------------------------------------------------

@owner_or_approver
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    request = (message.text or "").strip()
    if not request:
        return

    # "approve CODE" / "deny CODE" first — never planned as a request.
    parsed = parse_code_command(request)
    if parsed:
        verb, code = parsed
        await _handle_code(update, context, verb, code, via="text")
        return

    await handle_request(update, context, request)


def needs_external_planner(config: Config) -> bool:
    if config.planner_mode == "cloud":
        return True
    if config.planner_mode == "local" and config.use_ollama:
        return urlparse(config.ollama_base_url).hostname not in {"127.0.0.1", "::1"}
    return False


def external_destinations(config: Config) -> str:
    destinations = []
    if config.planner_mode == "cloud":
        destinations.append(config.cloud_url)
    if config.use_ollama and urlparse(config.ollama_base_url).hostname not in {"127.0.0.1", "::1"}:
        destinations.append(config.ollama_base_url)
    return ", ".join(destinations)


TOKEN_SHAPE_RE = re.compile(r"\d{5,}:[A-Za-z0-9_\-]{20,}")
TOKEN_REFUSAL = ("⚠️ That looks like a Telegram bot token. I did not read it or send it anywhere. "
                 "Revoke it in @BotFather, then save the new token on the PC with set_token.bat "
                 "or main.py --set-token.")


def looks_like_token(text: str | None) -> bool:
    """A token-shaped string must never reach a planner (the cloud planner is a third party)."""
    return bool(TOKEN_SHAPE_RE.search(text or ""))


async def handle_request(update: Update, context: ContextTypes.DEFAULT_TYPE, request: str,
                         *, from_voice: bool = False) -> None:
    message = update.effective_message
    user = update.effective_user
    if looks_like_token(request):
        audit.warning("Token-shaped text refused before planning", user_id=user.id)
        await message.reply_text(TOKEN_REFUSAL)
        return
    if len(request or "") > MAX_REQUEST_CHARS:
        # Refused before any planner (local or cloud) sees the text.
        await message.reply_text(
            f"That request is too long ({len(request)} characters; the limit is {MAX_REQUEST_CHARS}). "
            "Split it into smaller requests."
        )
        return
    if not context.application.bot_data["config"].is_allowed(user.id):
        await message.reply_text("Only a configured requester may create plans.")
        return
    config: Config = context.application.bot_data["config"]
    planner: Planner = (Planner(mode="keywords", workspace_root=config.workspace_root, max_steps=config.max_plan_steps)
                        if from_voice else context.application.bot_data["planner"])
    try:
        preference = MemoryStore(config.jobs_file.parent / "memory.json").get(user.id).get("language", "en")
    except ValueError:
        preference = "en"
    language = language_for(request, preference)
    # An external planner is a data transfer. Ask before sending request/context.
    raw_local = request.strip().startswith(("!", "ps:"))
    if not from_voice and not raw_local and needs_external_planner(config):
        destinations = external_destinations(config)
        context_summary = (f"OS: {platform.system()} {platform.release()}; home: {Path.home()}; "
                           f"working folder: {config.workspace_root}; current local date and time")
        approval = Plan(summary="Approve sending this request and system context to an external planner",
                        steps=[Step("cloud_plan", {"request": request, "destination": destinations,
                                                   "context": context_summary})], planner="direct")
        approval.language = language
        await propose_plan(update, context, approval)
        return
    audit.event("request", user_id=user.id, chat_id=update.effective_chat.id, text=request)

    panic_epoch = context.application.bot_data.get("panic_epoch", 0)
    thinking = await message.reply_text(MSG["thinking"])
    try:
        plan = await asyncio.to_thread(planner.plan, request)
    except Exception as exc:  # the planner should never raise; be defensive
        audit.error(f"Planner crashed: {exc}", exc_info=True)
        await thinking.edit_text(f"⚠️ Planning failed: {exc}")
        return

    if context.application.bot_data.get("panic_epoch", 0) != panic_epoch:
        await thinking.edit_text("Planning cancelled by /panic. Send a new request to continue.")
        return

    plan.language = language
    if from_voice:
        plan.transcript = request
    audit.event("plan", user_id=user.id, planner=plan.planner, model=plan.model,
                risk=plan.risk, note=plan.note, plan=render_plan_plain(plan))

    if not plan.needs_action:
        reply = plan.reply_text or plan.summary
        if language == "ar" and plan.planner == "keywords":
            reply = "لم أتمكن من فهم الطلب بدقة. أرسل التفاصيل أو اكتب المسار كاملاً، ثم حاول مرة أخرى."
        if from_voice:
            reply = ("النص المتعرف عليه: " if language == "ar" else "Recognized voice: ") + request + "\n\n" + reply
        answer = _esc(reply)
        try:
            await thinking.edit_text(answer, parse_mode=ParseMode.HTML)
        except TelegramError:
            await send_long(context.bot, update.effective_chat.id, answer,
                            parse_mode=ParseMode.HTML)
        return

    try:
        await thinking.delete()
    except TelegramError:
        pass
    await propose_plan(update, context, plan)


async def propose_plan(update: Update, context: ContextTypes.DEFAULT_TYPE,
                       plan: Plan) -> PendingPlan | None:
    """Create a pending plan (code + fingerprint) and send confirmations."""
    config: Config = context.application.bot_data["config"]
    store: dict[str, PendingPlan] = context.application.bot_data["plans"]
    user = update.effective_user
    chat_id = update.effective_chat.id
    if chat_id != user.id or not config.is_allowed(user.id):
        return None
    if not hasattr(plan, "language"):
        try:
            fallback = MemoryStore(config.jobs_file.parent / "memory.json").get(user.id).get("language", "en")
        except ValueError:
            fallback = "en"
        current_text = getattr(update.effective_message, "text", "") or ""
        plan.language = (language_for(current_text, fallback) if not current_text.lstrip().startswith('/')
                         or re.search(r'[\u0600-\u06ff]', current_text) else fallback)

    panic_epoch = context.application.bot_data.get("panic_epoch", 0)
    watch_epoch = context.application.bot_data.get("watch_epoch", 0)
    watch_plan = any(s.action in {"watch_start", "watch_speed"} for s in plan.steps)
    remote_plan = any(s.action == "remote_start" for s in plan.steps)
    remote_epoch = context.application.bot_data.get("remote_epoch", 0)
    versions = context.application.bot_data.setdefault("proposal_versions", {})
    proposal_key = (user.id, chat_id)
    version = versions.get(proposal_key, 0) + 1
    versions[proposal_key] = version
    _prune(store)
    # Re-planning invalidates the requester's previous pending plan (new code).
    for old in [p for p in store.values() if p.requester_id == user.id and p.chat_id == chat_id]:
        store.pop(old.plan_id, None)
        await _mark_messages(
            context, old,
            "♻️ <b>Superseded</b> — a newer plan replaced this one, so its code is void.",
            with_keyboard=False,
        )
    if len(store) >= MAX_PENDING_PLANS:
        await update.effective_message.reply_text(
            "⚠️ Too many requests are waiting for approval. Approve, deny or let some expire, then retry."
        )
        return None

    if (context.application.bot_data.get("panic_epoch", 0) != panic_epoch or
            versions.get(proposal_key) != version or
            (watch_plan and context.application.bot_data.get("watch_epoch", 0) != watch_epoch) or
            (remote_plan and context.application.bot_data.get("remote_epoch", 0) != remote_epoch)):
        return None
    code = unique_code([p.code for p in store.values()])
    pending = PendingPlan(
        plan_id=uuid.uuid4().hex[:12],
        code=code,
        fingerprint=fingerprint(plan.steps),
        requester_id=user.id,
        chat_id=chat_id,
        plan=plan,
        created_at=time.time(),
        watch_epoch=watch_epoch,
        remote_epoch=remote_epoch,
    )
    store[pending.plan_id] = pending

    requester_label = f"@{user.username}" if user.username else f"id {user.id}"
    approvers = sorted(config.approver_user_ids - {user.id})
    text = render_plan_message(
        plan,
        code=pending.code,
        fingerprint_value=pending.fingerprint,
        requester=requester_label if config.two_person_rule else "",
        approvers=approvers if config.two_person_rule else None,
    )
    # Never allow confirmation of a plan whose exact parameters cannot be
    # displayed in one Telegram message. Ask for smaller steps instead.
    if len(text.encode("utf-16-le")) // 2 > MAX_TG_LEN:
        store.pop(pending.plan_id, None)
        await update.effective_message.reply_text(
            "Plan is too long to show completely for safe approval. Please split your request into smaller steps."
        )
        return None
    keyboard = build_keyboard(pending.plan_id)

    # 1) the requester's chat
    try:
        sent = await context.bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
            reply_markup=keyboard, disable_web_page_preview=True,
        )
        pending.message_refs.append((chat_id, sent.message_id))
    except TelegramError as exc:
        audit.warning("Could not send plan message", error=str(exc))
        await send_long(context.bot, chat_id, text, parse_mode=ParseMode.HTML)

    # 2) every approver that is not the requester
    for approver_id in approvers:
        try:
            sent = await context.bot.send_message(
                chat_id=approver_id, text=text, parse_mode=ParseMode.HTML,
                reply_markup=keyboard, disable_web_page_preview=True,
            )
            pending.message_refs.append((approver_id, sent.message_id))
        except TelegramError as exc:
            audit.warning("Could not reach approver", approver_id=approver_id,
                          error=str(exc))

    if (context.application.bot_data.get("panic_epoch", 0) != panic_epoch or
            versions.get(proposal_key) != version or store.get(pending.plan_id) is not pending or
            (watch_plan and context.application.bot_data.get("watch_epoch", 0) != watch_epoch) or
            (remote_plan and context.application.bot_data.get("remote_epoch", 0) != remote_epoch)):
        store.pop(pending.plan_id, None)
        await _mark_messages(context, pending, "🛑 Cancelled by /panic or a newer request.")
        return None
    audit.event("plan_pending", code=pending.code, fingerprint=pending.fingerprint,
                requester=user.id, risk=plan.risk)
    return pending


# ---------------------------------------------------------------------------
# Approve / deny by code
# ---------------------------------------------------------------------------

async def _handle_code(update: Update, context: ContextTypes.DEFAULT_TYPE,
                       verb: str, code: str, via: str) -> None:
    config: Config = context.application.bot_data["config"]
    store: dict[str, PendingPlan] = context.application.bot_data["plans"]
    user = update.effective_user
    message = update.effective_message
    _prune(store)

    if not can_approve(config, user.id):
        audit.warning("Non-approver tried to confirm a plan", user_id=user.id, code=code)
        await message.reply_text(MSG["not_approver"], parse_mode=ParseMode.HTML)
        return

    pending = find_by_code(store, code)
    if pending is None:
        pending_codes = [p.code for p in store.values()]
        hint = (f" Pending codes: <code>{', '.join(pending_codes)}</code>"
                if pending_codes else " Nothing is waiting for confirmation.")
        audit.warning("Unknown/expired plan code", user_id=user.id, code=code, via=via)
        await message.reply_text(
            f"⌛ No live plan with code <code>{_esc(code)}</code>.{hint}\n"
            "Codes are single use and expire after 15 minutes.",
            parse_mode=ParseMode.HTML,
        )
        return

    if verb == "approve" and config.two_person_rule and pending.requester_id == user.id:
        await message.reply_text("A different configured approver must approve this plan.")
        return

    if pending.fingerprint != fingerprint(pending.plan.steps):
        # Should be impossible; refuse rather than execute something unexpected.
        store.pop(pending.plan_id, None)
        audit.error(f"Fingerprint mismatch for plan {pending.code}; refusing")
        await message.reply_text(
            "⚠️ The stored plan does not match its fingerprint, so I refused to run it. "
            "Please send the request again.",
        )
        return

    if verb == "approve":
        await _run_plan(update, context, pending, approved_by=user.id, via=via)
    else:
        store.pop(pending.plan_id, None)
        audit.event("deny", user_id=user.id, code=pending.code, via=via)
        await _mark_messages(
            context, pending,
            f"❌ <b>Denied by user {user.id}.</b> Nothing was executed.\n"
            f"<i>{_esc(pending.plan.summary)}</i>",
            with_keyboard=False,
        )
        if via != "button":
            await message.reply_text(MSG["cancelled"])


# ---------------------------------------------------------------------------
# Buttons
# ---------------------------------------------------------------------------

@owner_or_approver
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    config: Config = context.application.bot_data["config"]
    data = query.data or ""
    if ":" not in data:
        await query.answer()
        return
    verb, plan_id = data.split(":", 1)

    store: dict[str, PendingPlan] = context.application.bot_data["plans"]
    _prune(store)
    pending = store.get(plan_id)

    if not can_approve(config, query.from_user.id):
        audit.warning("Non-approver pressed a plan button",
                      user_id=query.from_user.id, plan_id=plan_id)
        try:
            await query.answer("Only a configured approver may confirm plans.", show_alert=True)
        except TelegramError:
            pass
        return

    if pending is None:
        await query.answer()
        try:
            await query.edit_message_text(MSG["expired"])
        except TelegramError:
            pass
        return

    if verb == "apv" and config.two_person_rule and pending.requester_id == query.from_user.id:
        await query.answer("A different approver must approve this plan.", show_alert=True)
        return

    await query.answer()
    if pending.fingerprint != fingerprint(pending.plan.steps):
        store.pop(pending.plan_id, None)
        await query.edit_message_text("Plan fingerprint mismatch; refused. Send the request again.")
        return

    # Make sure the message the button lives on gets updated too.
    origin = getattr(query, "message", None)
    if origin is not None:
        ref = (getattr(origin, "chat_id", None) or pending.chat_id,
               getattr(origin, "message_id", None))
        if ref[1] is not None and ref not in pending.message_refs:
            pending.message_refs.append(ref)

    if verb == "deny":
        store.pop(pending.plan_id, None)
        audit.event("deny", user_id=query.from_user.id, code=pending.code, via="button")
        await _mark_messages(
            context, pending,
            f"❌ <b>Denied.</b> Nothing was executed.\n<i>{_esc(pending.plan.summary)}</i>",
            with_keyboard=False,
        )
        return

    if verb == "apv":
        await _run_plan(update, context, pending, approved_by=query.from_user.id,
                        via="button")


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

class TelegramTransport:
    """Lets actions download Telegram files from the worker thread."""

    def __init__(self, bot: Any, loop: asyncio.AbstractEventLoop) -> None:
        self.bot = bot
        self.loop = loop

    def download_file(self, file_id: str, destination: Path) -> int:
        future = asyncio.run_coroutine_threadsafe(
            self._download(file_id, destination), self.loop
        )
        try:
            return future.result(timeout=300)
        except Exception:
            future.cancel()
            raise

    async def _download(self, file_id: str, destination: Path) -> int:
        tg_file = await self.bot.get_file(file_id)
        await tg_file.download_to_drive(custom_path=str(destination))
        return destination.stat().st_size if destination.exists() else 0


async def _mark_messages(context: ContextTypes.DEFAULT_TYPE, pending: PendingPlan,
                        text: str, with_keyboard: bool = False) -> None:
    """Edit every copy of a plan message (requester + approvers)."""
    if not pending.message_refs:
        audit.warning("No plan message to update", code=pending.code)
        return
    for chat_id, message_id in pending.message_refs:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text,
                parse_mode=ParseMode.HTML,
            )
        except TelegramError as exc:
            audit.warning("Could not update plan message", chat_id=chat_id,
                          message_id=message_id, error=str(exc))


async def _run_plan(update: Update, context: ContextTypes.DEFAULT_TYPE,
                    pending: PendingPlan, approved_by: int, via: str) -> None:
    store: dict[str, PendingPlan] = context.application.bot_data["plans"]

    # Claim atomically before the first await: a second click cannot replay it.
    if store.get(pending.plan_id) is not pending:
        return
    if context.application.bot_data.get("execution_active"):
        await update.effective_message.reply_text("Another plan is running; try again when it finishes.")
        return
    if pending.fingerprint != fingerprint(pending.plan.steps):
        store.pop(pending.plan_id, None)
        await update.effective_message.reply_text("Plan fingerprint mismatch; refused.")
        return
    store.pop(pending.plan_id, None)
    context.application.bot_data["execution_active"] = True
    clear_abort()
    try:
        await _execute_claimed_plan(update, context, pending, approved_by, via)
    finally:
        context.application.bot_data["execution_active"] = False


async def _execute_claimed_plan(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                pending: PendingPlan, approved_by: int, via: str) -> None:
    config: Config = context.application.bot_data["config"]
    jobs: JobStore | None = context.application.bot_data.get("jobs")
    audit.event(
        "approve", user_id=approved_by, code=pending.code,
        fingerprint=pending.fingerprint, via=via, risk=pending.plan.risk,
        plan=render_plan_plain(pending.plan),
    )
    await _mark_messages(
        context, pending,
        f"▶️ <b>Approved by user {approved_by}</b> (code "
        f"<code>{_esc(pending.code)}</code>) — running {len(pending.plan.steps)} step(s)…",
    )

    chat_id = pending.chat_id
    bot = context.bot
    loop = asyncio.get_running_loop()
    transport = TelegramTransport(bot, loop)
    started = time.perf_counter()
    succeeded = failed = 0
    aborted = False
    followup_plan = None
    epoch = context.application.bot_data.get("panic_epoch", 0)

    for index, step in enumerate(pending.plan.steps, start=1):
        if abort_requested():
            aborted = True
            break
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except TelegramError:
            pass

        audit.event("action_start", user_id=approved_by, code=pending.code, step=index,
                    action=step.action, risk=_risk(step.action),
                    params=loggable_params(step.params))
        if step.action == "cloud_plan":
            if (len(pending.plan.steps) != 1 or
                    step.params.get("destination") != external_destinations(config)):
                result = ActionResult("cloud_plan", False, "Planner destination changed; request again.")
            else:
                try:
                    followup_plan = await asyncio.to_thread(context.application.bot_data["planner"].plan, step.params["request"])
                    if context.application.bot_data.get("panic_epoch", 0) != epoch or abort_requested():
                        followup_plan = None
                        result = ActionResult("cloud_plan", False, "Cancelled by /panic.")
                    else:
                        followup_plan.language = getattr(pending.plan, "language", "en")
                        result = ActionResult("cloud_plan", True, "Planning finished. Review the next plan before any PC action.")
                except Exception:
                    result = ActionResult("cloud_plan", False, "External planning failed.")
        elif step.action in {"screenshot", "window_screenshot"}:
            result = await _run_visible_screenshot(step, pending, context, config, transport, jobs)
        elif step.action in {"watch_start", "watch_speed"}:
            if pending.watch_epoch != context.application.bot_data.get("watch_epoch", 0):
                result = ActionResult(step.action, False, "Sharing was stopped after this plan was proposed.")
            else:
                result = await _run_watch_action(step, pending, context, config)
        elif step.action == "remote_start":
            if pending.remote_epoch != context.application.bot_data.get("remote_epoch", 0):
                result = ActionResult(step.action, False, "Remote session was stopped after this plan was proposed.")
            else:
                result = await _run_remote_start(step, pending, context, config)
        else:
            result = await asyncio.to_thread(execute_step, step, config, transport, jobs, chat_id)
        audit.event("action_result", user_id=approved_by, code=pending.code, step=index,
                    action=step.action, ok=result.ok, duration=round(result.duration, 3),
                    output=result.output[:1000])
        if result.ok:
            succeeded += 1
        else:
            failed += 1
            if "panic" in result.output.lower():
                aborted = True

        icon = ACTION_ICONS.get(step.action, "•")
        header = (f"{index}/{len(pending.plan.steps)} {icon} "
                  f"<b>{_esc(step.action)}</b> {risk_badge(_risk(step.action))}")
        body = result.output or "(no output)"
        if getattr(pending.plan, "language", "en") == "ar":
            body = ("تم التنفيذ بنجاح. النتيجة الأصلية:\n" if result.ok else "تعذر التنفيذ. تفاصيل النتيجة:\n") + body
        payload = f"{header}\n{_esc(body)}" if result.files else \
                  f"{header}\n<pre>{_esc(body)}</pre>"
        await send_long(bot, chat_id, payload, parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True)

        for file_path in result.files:
            await _send_file(bot, chat_id, file_path, result.files_as_document, config,
                             is_screenshot=step.action in {"screenshot", "window_screenshot"})

        if aborted:
            break

    duration = time.perf_counter() - started
    if getattr(pending.plan, "language", "en") == "ar":
        verdict = "🛑 أُوقف التنفيذ" if aborted else ("✅ اكتمل التنفيذ" if failed == 0 else "⚠️ اكتمل مع أخطاء")
    else:
        verdict = "🛑 Aborted by /panic" if aborted else ("✅ Done" if failed == 0 else "⚠️ Finished with errors")
    summary = (
        f"{verdict} — {succeeded} ok, {failed} failed in {duration:.1f}s\n"
        f"<i>{_esc(pending.plan.summary)}</i> · code <code>{_esc(pending.code)}</code>"
    )
    audit.event("plan_finished", user_id=approved_by, code=pending.code, ok=succeeded,
                failed=failed, aborted=aborted, duration=round(duration, 2))
    await send_long(bot, chat_id, summary, parse_mode=ParseMode.HTML)
    if followup_plan is not None and not abort_requested():
        if any(p.requester_id == pending.requester_id for p in context.application.bot_data['plans'].values()):
            await send_long(bot, chat_id, "A newer request is pending; the external plan was discarded.")
        elif not followup_plan.needs_action:
            await send_long(bot, chat_id, followup_plan.reply_text or followup_plan.summary)
        else:
            fake_user = SimpleNamespace(id=pending.requester_id, username=None)
            fake_update = SimpleNamespace(effective_user=fake_user,
                effective_chat=SimpleNamespace(id=chat_id, type='private'), effective_message=update.effective_message)
            await propose_plan(fake_update, context, followup_plan)


async def _send_file(bot: Any, chat_id: int, path: Path, as_document: bool,
                    config: Config, *, is_screenshot: bool = False) -> None:
    try:
        with path.open("rb") as handle:
            if as_document:
                await bot.send_document(chat_id=chat_id, document=handle, filename=path.name)
            else:
                await bot.send_photo(chat_id=chat_id, photo=handle)
    except (OSError, TelegramError) as exc:
        audit.warning("Could not send file", path=str(path), error=str(exc))
        if isinstance(exc, OSError):
            # A locked or unreadable file: name the file only, and give a short reason
            # (the raw OS error carries the full path).
            text = (f"⚠️ Could not attach {_esc(path.name)}: "
                    f"could not read the file: {_esc(os_error_reason(exc))}.")
        else:
            text = f"⚠️ Could not attach {_esc(str(path))}: {_esc(str(exc))}"
        await send_long(bot, chat_id, text, parse_mode=ParseMode.HTML)
        return
    finally:
        # Legacy defensive cleanup: screenshot actions now use memory only.
        if is_screenshot:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Uploaded files (receive_file)
# ---------------------------------------------------------------------------

@owner_only
async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.application.bot_data["config"]
    message = update.effective_message
    document = message.document
    if document is None:
        return

    if document.file_size is None:
        # Without the size Telegram reports, the download could be unbounded: refuse.
        await message.reply_text("⚠️ Telegram did not say how big that file is, so I will not download it.")
        return
    size = document.file_size
    filename = sanitize_filename(document.file_name or f"file_{document.file_id[:8]}")
    limit = config.max_send_file_mb * 1024 * 1024
    if size > limit:
        await message.reply_text(
            f"⚠️ That file is {size / 1024 / 1024:.1f} MB; the limit is "
            f"{config.max_send_file_mb} MB."
        )
        return

    folder = config.downloads_dir
    try:
        folder.mkdir(parents=True, exist_ok=True)
        target = unique_destination(folder, filename)
    except OSError as exc:
        await message.reply_text(f"⚠️ The downloads folder is not usable: {exc}")
        return

    plan = Plan(
        summary=f"Save the uploaded file as {target}",
        steps=[Step(
            "receive_file",
            {
                "file_id": document.file_id,
                "file_name": filename,
                "size": str(size),
                "target": str(target),
            },
            "download the file into the downloads folder",
        )],
        planner="direct",
    )
    audit.event("upload_received", user_id=update.effective_user.id,
                file_name=filename, size=size, target=str(target))
    await propose_plan(update, context, plan)


# ---------------------------------------------------------------------------
# Confirmed personal memory, macros and daily briefings
# ---------------------------------------------------------------------------

def _memory(config: Config) -> MemoryStore:
    return MemoryStore(config.jobs_file.parent / 'memory.json')


async def _direct(update, context, summary, action, params):
    await propose_plan(update, context, Plan(summary=summary, steps=[Step(action, params)], planner='direct'))


@owner_only
async def cmd_note(update, context):
    text = ' '.join(context.args or []).strip()
    if not text or len(text) > 1000:
        await update.effective_message.reply_text('Usage: /note TEXT (up to 1000 characters)')
        return
    await _direct(update, context, 'Save a local note', 'memory_update', {'op': 'add_note', 'value': text})


@owner_only
async def cmd_notes(update, context):
    await _direct(update, context, 'Send saved notes to this chat', 'memory_read', {})


@owner_only
async def cmd_forget(update, context):
    args = context.args or []
    if len(args) == 1 and args[0].lower() in {'language', 'all'}:
        op = 'forget_language' if args[0].lower() == 'language' else 'forget_all'
        await _direct(update, context, 'Remove language preference' if op == 'forget_language' else 'Remove all notes, macros and preferences', 'memory_update', {'op': op})
        return
    if len(args) != 1 or not args[0].isdigit():
        await update.effective_message.reply_text('Usage: /forget NOTE_NUMBER, /forget language, or /forget all')
        return
    config = context.application.bot_data['config']
    try:
        notes = _memory(config).get(update.effective_user.id).get('notes', [])
        n = int(args[0])
        if not 1 <= n <= len(notes):
            raise ValueError('Note number not found.')
        expected = notes[n - 1]
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    await _direct(update, context, 'Delete the displayed note', 'memory_update', {'op': 'forget_note', 'value': str(n), 'steps': expected})


@owner_only
async def cmd_language(update, context):
    args = context.args or []
    if len(args) != 1 or args[0].lower() not in {'en', 'ar'}:
        await update.effective_message.reply_text('Usage: /language en or /language ar')
        return
    await _direct(update, context, 'Save language preference', 'memory_update', {'op': 'set_language', 'value': args[0].lower()})


@owner_only
async def cmd_briefing(update, context):
    args = context.args or []
    if not args or args == ['now']:
        await _direct(update, context, 'Read and send a briefing', 'briefing', {})
    elif len(args) == 2 and args[0].lower() == 'daily':
        from .jobs import TIME_ONLY_RE, parse_when
        try:
            if not TIME_ONLY_RE.fullmatch(args[1]):
                raise ValueError('Use HH:MM.')
            parse_when(args[1])
        except ValueError:
            await update.effective_message.reply_text('Usage: /briefing daily HH:MM (local 24-hour time)')
            return
        await _direct(update, context, 'Schedule a daily briefing approval', 'briefing_schedule', {'when': args[1]})
    else:
        await update.effective_message.reply_text('Usage: /briefing now  or  /briefing daily HH:MM')


@owner_only
async def cmd_macros(update, context):
    await _direct(update, context, 'Send your saved macro names', 'memory_read', {'kind': 'macros'})


@owner_only
async def cmd_chain(update, context):
    text = ' '.join(context.args or []).strip()
    parts = [part.strip() for part in text.split(';')]
    config = context.application.bot_data['config']
    if not text or any(not part for part in parts) or len(parts) > config.max_plan_steps:
        await update.effective_message.reply_text('Usage: /chain open chrome ; take a screenshot (1–6 clear steps)')
        return
    planner = Planner(mode='keywords', workspace_root=config.workspace_root, max_steps=config.max_plan_steps)
    steps = []
    for part in parts:
        plan = await asyncio.to_thread(planner.plan, part)
        if len(plan.steps) != 1 or not plan.needs_action:
            await update.effective_message.reply_text('A chain step was not understood; please rewrite it. Nothing was run.')
            return
        steps.extend(plan.steps)
    plan = Plan(summary='Run the displayed steps in order', steps=steps, planner='direct')
    plan.language = language_for(text)
    await propose_plan(update, context, plan)


@owner_only
async def cmd_macro(update, context):
    args = context.args or []
    if len(args) < 2 or args[0].lower() not in {'save', 'run', 'delete'}:
        await update.effective_message.reply_text('Usage: /macro save NAME, /macro run NAME, /macro delete NAME')
        return
    verb, name = args[0].lower(), ' '.join(args[1:]).strip()
    if not NAME_RE.fullmatch(name):
        await update.effective_message.reply_text('Use a macro name of 1–40 letters, numbers, spaces, underscores or hyphens.')
        return
    config = context.application.bot_data['config']
    user_id = update.effective_user.id
    if verb == 'save':
        store = context.application.bot_data['plans']
        _prune(store)
        existing = [p for p in store.values() if p.requester_id == user_id and p.chat_id == update.effective_chat.id]
        if not existing:
            await update.effective_message.reply_text('First request a multi-step plan, then send /macro save NAME before approving it.')
            return
        plan = existing[-1].plan
        if any(step.action in {'memory_update', 'briefing_schedule', 'job_cancel', 'remote_start', 'app_install', 'app_uninstall'} for step in plan.steps):
            await update.effective_message.reply_text('Personal-memory, scheduler, remote-session and app install/remove steps cannot be saved as macros.')
            return
        steps = json.dumps([{'action': st.action, 'params': st.params} for st in plan.steps], ensure_ascii=False, sort_keys=True)
        await _direct(update, context, 'Save the following steps as a macro', 'memory_update', {'op': 'save_macro', 'name': name, 'steps': steps})
        return
    try:
        stored = _memory(config).get(user_id).get('macros', {})
        if name not in stored:
            await update.effective_message.reply_text('Macro not found.')
            return
        if verb == 'delete':
            await _direct(update, context, 'Delete the named macro', 'memory_update', {'op': 'delete_macro', 'name': name})
            return
        raw = json.dumps({'summary': f'Run macro {name}', 'steps': stored[name]}, ensure_ascii=False)
        plan = build_plan(raw, 'direct', '', config.max_plan_steps, workspace_root=None)
        if len(plan.steps) != len(stored[name]):
            raise ValueError('Macro exceeds the step limit.')
    except (ValueError, PlannerError, TypeError):
        await update.effective_message.reply_text('Stored macro is invalid; it was not run.')
        return
    await propose_plan(update, context, plan)


@owner_only
async def on_voice(update, context):
    message = update.effective_message
    voice = message.voice
    if voice is None:
        return
    config = context.application.bot_data['config']
    if (not voice.file_size or voice.file_size > MAX_VOICE_BYTES or
            not voice.duration or voice.duration > MAX_VOICE_SECONDS):
        await message.reply_text('Voice notes must be at most 5 MB and 2 minutes; try a shorter recording.')
        return
    epoch = context.application.bot_data.get('panic_epoch', 0)
    try:
        tg_file = await context.bot.get_file(voice.file_id)
        audio = bytes(await tg_file.download_as_bytearray())
        if len(audio) > MAX_VOICE_BYTES:
            raise VoiceUnavailable('Voice note exceeds 5 MB.')
        text = await asyncio.to_thread(transcribe, audio, config.voice_model_path)
    except VoiceUnavailable as exc:
        await message.reply_text(str(exc))
        return
    except Exception:
        audit.warning('Voice download failed')
        await message.reply_text('Could not download the voice note; please try again.')
        return
    if context.application.bot_data.get('panic_epoch', 0) != epoch:
        await message.reply_text('Voice request cancelled by /panic.')
        return
    if parse_code_command(text) or text.lstrip().startswith('/'):
        await message.reply_text('For safety, send approval codes and bot commands as text, not voice.')
        return
    # Local-only keyword planner; the transcript is never sent to the cloud planner.
    await message.reply_text('Voice received and transcribed locally. Planning with offline keywords; please review the resulting plan.')
    await handle_request(update, context, text, from_voice=True)


async def queue_briefing(application, job):
    config = application.bot_data['config']
    if not config.is_allowed(job.chat_id) or abort_requested():
        return False
    if application.bot_data.get('paused'):
        return False  # the job stays due and is queued after /resume
    store = application.bot_data['plans']
    _prune(store)
    if any(p.requester_id == job.chat_id for p in store.values()):
        return False  # never replace a different pending request
    user = SimpleNamespace(id=job.chat_id, username=None)
    update = SimpleNamespace(effective_user=user, effective_chat=SimpleNamespace(id=job.chat_id, type='private'),
                             effective_message=SimpleNamespace(text=''))
    context = SimpleNamespace(application=application, bot=application.bot)
    if job.kind == 'briefing':
        plan = Plan(summary='Daily briefing: approve before sending PC status, reminders and notes',
                    steps=[Step('briefing', {})], planner='scheduled')
    elif job.kind == 'reminder':
        plan = Plan(summary='Reminder is due: approve delivery', steps=[Step('reminder_delivery', {'text': job.text})], planner='scheduled')
    elif job.kind == 'task':
        plan = Plan(summary='Scheduled command is due: approve execution', steps=[Step('powershell', {'command': job.command})], planner='scheduled')
    else:
        return False
    return await propose_plan(update, context, plan) is not None



def _watch_controller(context, config):
    data = context.application.bot_data
    if 'watch_controller' not in data:
        data['watch_controller'] = WatchController(context.bot, config)
    return data['watch_controller']


async def _run_watch_action(step, pending, context, config):
    if pending.chat_id != pending.requester_id or not config.is_allowed(pending.requester_id):
        return ActionResult(step.action, False, 'Sharing is restricted to an allowed private chat.')
    controller = _watch_controller(context, config)
    try:
        seconds = int(step.params.get('seconds', ''))
        if step.action == 'watch_start':
            minutes = int(step.params.get('minutes', ''))
            ok, message = await controller.start(pending.requester_id, pending.chat_id, seconds, minutes)
        else:
            ok, message = controller.speed(pending.requester_id, step.params.get('session', ''), seconds)
    except (ValueError, TypeError):
        return ActionResult(step.action, False, 'Invalid sharing interval or duration.')
    return ActionResult(step.action, ok, message)


async def _run_visible_screenshot(step, pending, context, config, transport, jobs):
    if pending.chat_id != pending.requester_id or not config.is_allowed(pending.requester_id):
        return ActionResult(step.action, False, 'Screen photos are restricted to an allowed private chat.')
    if step.params.get('path'):
        return ActionResult(step.action, False, 'Saving screenshots to disk is disabled; request a photo instead.')
    if abort_requested():
        return ActionResult(step.action, False, 'Aborted by /panic.')
    window_handle = foreground_window_handle() if step.action == 'window_screenshot' else 0
    lease = CaptureLease()
    lease.task = asyncio.current_task()
    context.application.bot_data['screen_capture_lease'] = lease
    monitor = None
    try:
        lease.indicator = await ScreenIndicator.open()
        if lease.stopping or not lease.indicator.visible() or abort_requested():
            return ActionResult(step.action, False, 'Visible indicator unavailable; no screen capture was taken.')
        monitor = asyncio.create_task(lease.monitor())
        if step.action == 'screenshot':
            data = await asyncio.wait_for(capture_frame(lease, 2560, 84), 45)
        else:
            data = await asyncio.wait_for(capture_frame(lease, 2560, 84,
                                          step.params.get('title', '').strip(), window_handle), 45)
        result = ActionResult(step.action, True, 'Approved photo captured in memory.', photo=data)
        del data
        if lease.stopping or abort_requested() or not lease.indicator.visible():
            return ActionResult(step.action, False, 'Capture discarded because sharing stopped or the indicator closed.')
        if not result.ok or result.photo is None:
            return result
        if not config.is_allowed(pending.requester_id):
            return ActionResult(step.action, False, 'The requester is no longer allowed; photo discarded.')
        stream = photo_stream(result.photo)
        try:
            await context.bot.send_photo(chat_id=pending.chat_id, photo=stream,
                                         caption='📸 Approved screen photo' if step.action == 'screenshot'
                                         else '🪟 Approved window photo')
        except Exception:
            return ActionResult(step.action, False, 'Could not send the screen photo; it was discarded.')
        finally:
            stream.close()
            result.photo = None
        return result
    except asyncio.CancelledError:
        if not lease.stopping:
            raise
        return ActionResult(step.action, False, 'Screen capture stopped; a transmission already in flight may have reached Telegram.')
    except Exception:
        return ActionResult(step.action, False, 'Screen capture failed or the required indicator was unavailable; no photo was sent.')
    finally:
        if monitor:
            monitor.cancel()
        if lease.indicator is not None:
            await lease.indicator.close()
        if context.application.bot_data.get('screen_capture_lease') is lease:
            context.application.bot_data.pop('screen_capture_lease', None)


def _user_language(config: Config, user_id: int) -> str:
    try:
        lang = _memory(config).get(user_id).get("language", "en")
    except ValueError:
        return "en"
    return lang if lang in {"en", "ar"} else "en"


@owner_only
async def cmd_health(update, context):
    """Read-only PC check (Round 9). No approval: it changes nothing."""
    if context.args:
        await update.effective_message.reply_text("Usage: /health")
        return
    config: Config = context.application.bot_data["config"]
    language = _user_language(config, update.effective_user.id)
    await update.effective_message.reply_text("🩺 Checking this PC (read-only)…")
    checks = await asyncio.to_thread(health.run_health)
    await update.effective_message.reply_text(
        health.render(checks, language), parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def _app_request(update, context, action: str) -> None:
    """Shared path for /install and /uninstall: allow-list check, then a HIGH RISK approval."""
    config: Config = context.application.bot_data["config"]
    verb = "install" if action == "app_install" else "uninstall"
    apps = app_allowlist.allowed_apps(config.extra_allowed_apps)
    args = context.args or []
    if len(args) != 1:
        await update.effective_message.reply_text(
            f"Usage: /{verb} NAME. Allowed names: {app_allowlist.names_text(apps)}"
        )
        return
    if looks_like_token(args[0]):
        await update.effective_message.reply_text(TOKEN_REFUSAL)
        return
    name = args[0].lower()
    entry = apps.get(name)
    if entry is None:
        await update.effective_message.reply_text(
            f"'{_esc(args[0][:40])}' is not on the app allow-list, so nothing was run. "
            f"Allowed names: {_esc(app_allowlist.names_text(apps))}. To add an app, add "
            "name=winget.id to extra_allowed_apps under [apps] in config.ini (use an ID you trust), "
            "then restart the bot.",
            parse_mode=ParseMode.HTML,
        )
        return
    if winget_path() is None:
        await update.effective_message.reply_text(
            "winget is not available on this PC, so nothing was run. Install or update "
            "\"App Installer\" from the Microsoft Store, then try again."
        )
        return
    title, app_id = entry["title"], entry["id"]
    if action == "app_install":
        summary = (f"Install {title} with winget: exact id {app_id}, source winget only, latest version. "
                   "HIGH RISK. A UAC prompt will appear on this PC's screen and must be accepted there.")
    else:
        summary = (f"Remove {title} with winget: exact id {app_id}, source winget only. "
                   "HIGH RISK. A UAC prompt will appear on this PC's screen and must be accepted there.")
    await _direct(update, context, summary, action, {"name": name, "id": app_id})


@owner_only
async def cmd_install(update, context):
    await _app_request(update, context, "app_install")


@owner_only
async def cmd_uninstall(update, context):
    await _app_request(update, context, "app_uninstall")


@owner_only
async def cmd_screen(update, context):
    if context.args:
        await update.effective_message.reply_text('Usage: /screen')
        return
    await _direct(update, context, 'Take and send one full-screen photo with a visible indicator', 'screenshot', {})


@owner_only
async def cmd_window(update, context):
    title = ' '.join(context.args or []).strip()
    if len(title) > 200:
        await update.effective_message.reply_text('Window title must be at most 200 characters.')
        return
    params = {'title': title} if title else {}
    await _direct(update, context, 'Take and send one window photo with a visible indicator', 'window_screenshot', params)


@owner_only
async def cmd_watch(update, context):
    args = context.args or []
    if not args:
        await update.effective_message.reply_text('Usage: /watch start [seconds] [minutes], /watch speed N, /watch stop')
        return
    op = args[0].lower()
    if op == 'stop' and len(args) == 1:
        data = context.application.bot_data
        data['watch_epoch'] = data.get('watch_epoch', 0) + 1
        controller = data.get('watch_controller')
        stopped = controller.stop_now('requested') if controller else False
        store = data['plans']
        cancelled = [p for p in list(store.values()) if p.requester_id == update.effective_user.id
                     and any(st.action in {'watch_start', 'watch_speed'} for st in p.plan.steps)]
        for pending in cancelled:
            store.pop(pending.plan_id, None)
        for pending in cancelled:
            await _mark_messages(context, pending, '⏹ Screen sharing request cancelled.')
        await update.effective_message.reply_text('⏹ Screen sharing stopped or cancelled.' if stopped or cancelled
                                                  else 'No screen sharing is running or pending.')
        return
    if op == 'start' and 1 <= len(args) <= 3:
        try:
            seconds = int(args[1]) if len(args) > 1 else DEFAULT_INTERVAL
            minutes = int(args[2]) if len(args) > 2 else DEFAULT_MINUTES
            if not MIN_INTERVAL <= seconds <= MAX_INTERVAL or not 1 <= minutes <= MAX_MINUTES:
                raise ValueError
        except ValueError:
            await update.effective_message.reply_text('Use /watch start [2–60 seconds] [1–60 minutes].')
            return
        controller = context.application.bot_data.get('watch_controller')
        if controller is not None and controller.active is not None:
            await update.effective_message.reply_text('A screen-sharing session is already active or starting.')
            return
        await _direct(update, context, 'Start visible full-screen sharing to this chat', 'watch_start',
                      {'seconds': str(seconds), 'minutes': str(minutes)})
        return
    if op == 'speed' and len(args) == 2:
        try:
            seconds = int(args[1])
            if not MIN_INTERVAL <= seconds <= MAX_INTERVAL:
                raise ValueError
        except ValueError:
            await update.effective_message.reply_text('Use /watch speed N where N is 2–60 seconds.')
            return
        controller = context.application.bot_data.get('watch_controller')
        session = controller.active if controller else None
        if session is None or session.stopping or session.user_id != update.effective_user.id:
            await update.effective_message.reply_text('No active screen-sharing session in this chat.')
            return
        await _direct(update, context, 'Change speed of the active screen-sharing session', 'watch_speed',
                      {'seconds': str(seconds), 'session': session.id})
        return
    await update.effective_message.reply_text('Usage: /watch start [seconds] [minutes], /watch speed N, /watch stop')



def _remote_controller(context, config) -> RemoteController:
    data = context.application.bot_data
    if "remote_controller" not in data:
        factory = data.get("remote_deps_factory") or (
            lambda chat_id: make_remote_deps(context.bot, config, chat_id))
        platform_ok = data.get("remote_platform_ok") or (lambda: sys.platform == "win32")
        data["remote_controller"] = RemoteController(
            is_allowed=config.is_allowed, max_minutes=config.remote_max_minutes,
            default_minutes=config.remote_default_minutes, deps_factory=factory,
            platform_ok=platform_ok,
        )
    return data["remote_controller"]


async def _run_remote_start(step, pending, context, config):
    """Runs only after an approver confirmed the plan. The link is sent in its own message."""
    if pending.chat_id != pending.requester_id or not config.is_allowed(pending.requester_id):
        return ActionResult(step.action, False, "Remote sessions are restricted to the allowed private chat.")
    try:
        minutes = int(step.params.get("minutes", ""))
    except (ValueError, TypeError):
        return ActionResult(step.action, False, "Invalid remote session duration.")
    controller = _remote_controller(context, config)
    ok, message = await controller.start(pending.requester_id, pending.chat_id, minutes)
    return ActionResult(step.action, ok, message)


REMOTE_USAGE = ("Usage: /remote start [minutes] (1–60, default 15) · /remote status · /remote stop")


@owner_only
async def cmd_remote(update, context):
    config: Config = context.application.bot_data["config"]
    message = update.effective_message
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    args = [arg.lower() for arg in (context.args or [])]
    controller = _remote_controller(context, config)
    if not args:
        await message.reply_text(REMOTE_USAGE)
        return
    op = args[0]
    if op == "status" and len(args) == 1:
        await message.reply_text(controller.status_text())
        return
    if op == "stop" and len(args) == 1:
        data = context.application.bot_data
        data["remote_epoch"] = data.get("remote_epoch", 0) + 1
        store = data["plans"]
        cancelled = [p for p in list(store.values()) if p.requester_id == user_id
                     and any(st.action == "remote_start" for st in p.plan.steps)]
        for pending in cancelled:
            store.pop(pending.plan_id, None)
        for pending in cancelled:
            await _mark_messages(context, pending, "⏹ Remote session request cancelled.")
        stopped = await controller.stop("requested")
        if stopped:
            await message.reply_text("⏹ Remote session stopped. The link no longer works.")
        elif cancelled:
            await message.reply_text("No remote session was running; the pending request was cancelled.")
        else:
            await message.reply_text("No remote session is running.")
        return
    if op == "start" and len(args) <= 2:
        if chat_id != user_id:
            await message.reply_text("Remote sessions only work in a private chat with the bot.")
            return
        try:
            minutes = int(args[1]) if len(args) == 2 else config.remote_default_minutes
        except ValueError:
            minutes = -1
        refusal = controller.check_request(user_id, chat_id, minutes)
        if refusal is not None:
            await message.reply_text(refusal)
            return
        summary = (f"FULL VIEW AND CONTROL for {minutes} minute(s): you will see this PC's screen, and your "
                   "mouse and keyboard will control it until the time ends. A red banner stays on the PC; "
                   "Ctrl+Alt+Shift+Q or its Stop button ends the session early.")
        plan = Plan(summary=summary, steps=[Step(
            "remote_start", {"minutes": str(minutes)},
            description=f"Full view and mouse/keyboard control for {minutes} minute(s); stops automatically.",
        )], planner="direct")
        await propose_plan(update, context, plan)
        return
    await message.reply_text(REMOTE_USAGE)


# ---------------------------------------------------------------------------
# Errors and lifecycle
# ---------------------------------------------------------------------------

def _error_label(exc: BaseException | None) -> str:
    """A safe, lowercase label for the error class: "conflict", "network error"...

    Only the class name is used (never its message, which can contain URLs or
    request data). Both ``TimedOut`` and ``Conflict`` become readable labels.
    """
    if exc is None:
        return "unknown"
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", type(exc).__name__).lower()
    return name if re.fullmatch(r"[a-z][a-z .\-]{0,39}", name) else "unknown"


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    audit.error("Telegram handler error", exc_info=True,
                reason=_error_label(context.error))
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ Internal error while handling that update. Check the log file.",
            )
        except TelegramError:
            pass


async def _post_init(application: Application) -> None:
    bot = application.bot
    try:
        await bot.set_my_commands([
            BotCommand("start", "intro / how to use"),
            BotCommand("help", "all commands"),
            BotCommand("status", "planner, approvers, jobs"),
            BotCommand("privacy", "where my request text goes"),
            BotCommand("jobs", "reminders and scheduled tasks"),
            BotCommand("canceltask", "delete a reminder/task"),
            BotCommand("id", "show my Telegram user id"),
            BotCommand("cancel", "drop my pending plans"),
            BotCommand("pause", "hold all new work until /resume"),
            BotCommand("resume", "continue after /pause"),
            BotCommand("panic", "cancel everything and abort"),
            BotCommand("logs", "tail the local audit log"),
            BotCommand("note", "save a note (approval needed)"),
            BotCommand("notes", "show saved notes (approval needed)"),
            BotCommand("forget", "delete a note (approval needed)"),
            BotCommand("language", "set language preference"),
            BotCommand("briefing", "request or schedule a briefing"),
            BotCommand("chain", "propose several steps as one plan"),
            BotCommand("macro", "save, run or delete a macro"),
            BotCommand("macros", "list saved macros"),
            BotCommand("screen", "approve a full-screen photo"),
            BotCommand("window", "approve a window photo"),
            BotCommand("watch", "start, adjust or stop visible screen sharing"),
            BotCommand("remote", "private remote desktop (needs approval)"),
            BotCommand("health", "read-only PC health check"),
            BotCommand("install", "install an allow-listed app (needs approval)"),
            BotCommand("uninstall", "remove an allow-listed app (needs approval)"),
        ])
    except TelegramError as exc:
        audit.warning("Could not set bot commands", error=str(exc))

    config: Config = application.bot_data["config"]
    planner: Planner = application.bot_data["planner"]
    me = await bot.get_me()
    info = await asyncio.to_thread(planner.status)
    print(f"✅ Connected as @{me.username} (id {me.id})")
    if application.bot_data.get("paused"):
        since = (application.bot_data.get("paused_info") or {}).get("since", "unknown")
        print(f"⏸  Paused since {since}. Send /resume in Telegram to continue.")
    print(f"   Allowed (may ask)   : {sorted(config.allowed_user_ids)}")
    print(f"   Approvers (may say yes): {sorted(config.approver_user_ids)}")
    print(f"   Planner mode        : {info['mode']}  (chain: {' → '.join(info['chain'])})")
    if "cloud" in info:
        cloud = info["cloud"]
        print(f"   Cloud planner       : {cloud['model']} @ {cloud['url']} ({cloud['detail']})")
        if info["mode"] == "cloud":
            print("   ⚠️  Privacy          : request text is sent to pollinations.ai "
                  "(see /privacy in Telegram)")
    if "local" in info:
        print(f"   Local planner       : {info['local']['detail']}")
    print(f"   Actions             : {len(VALID_ACTIONS)} · step cap {config.max_plan_steps}")
    print(f"   Working folder      : {config.workspace_root}")
    print(f"   Log file            : {config.log_file}")
    print(f"   Jobs file           : {config.jobs_file}")
    print(f"   Token source        : {config.token_source or 'unknown'}")

    jobs_store = JobStore(config.jobs_file)
    application.bot_data["jobs"] = jobs_store
    application.bot_data["scheduler_task"] = asyncio.create_task(
        scheduler_loop(jobs_store, bot, config, lambda job: queue_briefing(application, job))
    )
    audit.event("bot_started", username=me.username, bot_id=me.id,
                planner=info["mode"], chain=info["chain"],
                jobs=len(jobs_store.all()), approvers=sorted(config.approver_user_ids))


async def _post_shutdown(application: Application) -> None:
    lease = application.bot_data.get("screen_capture_lease")
    if lease is not None:
        lease.stop_now()
    controller = application.bot_data.get("watch_controller")
    if controller is not None and controller.active is not None:
        session = controller.active
        controller.stop_now("assistant shutdown")
        try:
            await asyncio.wait_for(asyncio.shield(session.task), 5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    remote = application.bot_data.get("remote_controller")
    if remote is not None and remote.active is not None:
        try:
            await asyncio.wait_for(remote.stop("shutdown"), 10)
        except (asyncio.TimeoutError, Exception):
            audit.warning("Remote session did not stop cleanly during shutdown")
    task = application.bot_data.get("scheduler_task")
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    audit.close()  # release the log file so it can be rotated/deleted (Windows)
    clear_abort()


def build_application(config: Config) -> Application:
    planner = Planner.from_config(config)
    # Every message the bot sends passes the outbound redaction in assistant/outbound.py.
    # The token is registered here as well, so that holds even if the config was built
    # without the loader that normally registers it.
    audit.register_secret(config.bot_token)

    application = (
        Application.builder().concurrent_updates(True)
        .token(config.bot_token)
        .request(RedactingHTTPXRequest(connection_pool_size=256))
        .get_updates_request(RedactingHTTPXRequest(connection_pool_size=1))
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    application.bot_data["config"] = config
    application.bot_data["planner"] = planner
    application.bot_data["plans"] = {}
    # The pause survives a restart: it is read here, before any update is handled.
    application.bot_data["pause_file"] = pause_path(config.jobs_file)
    saved_pause = load_pause(application.bot_data["pause_file"])
    application.bot_data["paused"] = saved_pause is not None
    application.bot_data["paused_info"] = saved_pause

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler(["id", "whoami"], cmd_id))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("privacy", cmd_privacy))
    application.add_handler(CommandHandler("jobs", cmd_jobs))
    application.add_handler(CommandHandler("canceltask", cmd_canceltask))
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    application.add_handler(TypeHandler(Update, pause_gate), group=-1)
    application.add_handler(CommandHandler("pause", cmd_pause))
    application.add_handler(CommandHandler("resume", cmd_resume))
    application.add_handler(CommandHandler("panic", cmd_panic))
    application.add_handler(CommandHandler("logs", cmd_logs))
    application.add_handler(CommandHandler("note", cmd_note))
    application.add_handler(CommandHandler("notes", cmd_notes))
    application.add_handler(CommandHandler("forget", cmd_forget))
    application.add_handler(CommandHandler("language", cmd_language))
    application.add_handler(CommandHandler("briefing", cmd_briefing))
    application.add_handler(CommandHandler("chain", cmd_chain))
    application.add_handler(CommandHandler("macro", cmd_macro))
    application.add_handler(CommandHandler("macros", cmd_macros))
    application.add_handler(CommandHandler("screen", cmd_screen))
    application.add_handler(CommandHandler("window", cmd_window))
    application.add_handler(CommandHandler("watch", cmd_watch))
    application.add_handler(CommandHandler("remote", cmd_remote))
    application.add_handler(CommandHandler("health", cmd_health))
    application.add_handler(CommandHandler("install", cmd_install))
    application.add_handler(CommandHandler("uninstall", cmd_uninstall))
    application.add_handler(CommandHandler(["yes", "approve"], cmd_yes))
    application.add_handler(CommandHandler(["no", "deny"], cmd_no))
    application.add_handler(CallbackQueryHandler(on_button, pattern=r"^(apv|deny):"))
    application.add_handler(MessageHandler(filters.Document.ALL, on_document))
    application.add_handler(MessageHandler(filters.VOICE, on_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    application.add_error_handler(on_error)
    return application
