"""Single source of truth for the action vocabulary.

Everything that both the planner (validation, prompting) and the executor
(dispatch, risk labels, UI) needs to know about an action lives here, so the
three never drift apart.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Risk labels
# ---------------------------------------------------------------------------

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"

RISK_ORDER = {RISK_LOW: 0, RISK_MEDIUM: 1, RISK_HIGH: 2}

RISK_ICONS = {RISK_LOW: "🟢", RISK_MEDIUM: "🟡", RISK_HIGH: "🔴"}

RISK_EXPLANATIONS = {
    RISK_LOW: "may still reveal data or open software",
    RISK_MEDIUM: "changes local data or shares information",
    RISK_HIGH: "commands or system actions can cause data loss",
}

# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

#: Every action the assistant understands.
VALID_ACTIONS: tuple[str, ...] = (
    "reply",
    # --- system information (read only) --------------------------------
    "system_info",
    "list_processes",
    # --- desktop ------------------------------------------------------
    "open",
    "open_url",
    "screenshot",
    "window_screenshot",
    "lock_screen",
    "notification",
    "volume",
    "clipboard_get",
    "clipboard_set",
    # --- files --------------------------------------------------------
    "read_file",
    "write_file",
    "list_dir",
    "find_files",
    "send_file",
    "receive_file",
    # --- automation ---------------------------------------------------
    "reminder",
    "scheduled_task",
    "memory_update", "memory_read", "briefing_schedule", "briefing",
    "job_cancel", "jobs_read", "audit_read", "reminder_delivery", "cloud_plan",
    # --- dangerous ----------------------------------------------------
    "powershell",
    "kill_process",
    "power",
)

#: Exact params each action accepts. Anything else is rejected.
PARAM_SPEC: dict[str, dict[str, list[str]]] = {
    "reply": {"required": ["text"], "optional": []},
    # information
    "system_info": {"required": [], "optional": ["sections"]},
    "list_processes": {"required": [], "optional": ["sort", "limit"]},
    # desktop
    "open": {"required": ["target"], "optional": []},
    "open_url": {"required": ["url"], "optional": []},
    "screenshot": {"required": [], "optional": ["path"]},
    "window_screenshot": {"required": [], "optional": ["title", "path"]},
    "lock_screen": {"required": [], "optional": []},
    "notification": {"required": ["text"], "optional": ["title"]},
    "volume": {"required": [], "optional": ["mode", "level", "steps"]},
    "clipboard_get": {"required": [], "optional": []},
    "clipboard_set": {"required": ["text"], "optional": []},
    # files
    "read_file": {"required": ["path"], "optional": []},
    "write_file": {"required": ["path", "content"], "optional": []},
    "list_dir": {"required": [], "optional": ["path"]},
    "find_files": {"required": [], "optional": ["root", "name", "ext", "days", "limit"]},
    "send_file": {"required": ["path"], "optional": []},
    "receive_file": {"required": ["file_id", "file_name"], "optional": ["target", "size"]},
    # automation
    "reminder": {"required": ["text", "when"], "optional": []},
    "scheduled_task": {"required": ["when", "command"], "optional": ["repeat", "name"]},
    "memory_update": {"required": ["op"], "optional": ["value", "name", "steps"]},
    "memory_read": {"required": [], "optional": ["kind"]},
    "briefing_schedule": {"required": ["when"], "optional": []},
    "briefing": {"required": [], "optional": []},
    "job_cancel": {"required": ["id"], "optional": []},
    "jobs_read": {"required": [], "optional": []},
    "audit_read": {"required": [], "optional": []},
    "reminder_delivery": {"required": ["text"], "optional": []},
    "cloud_plan": {"required": ["request", "destination", "context"], "optional": []},
    # dangerous
    "powershell": {"required": ["command"], "optional": []},
    "kill_process": {"required": [], "optional": ["name", "pid", "force"]},
    "power": {"required": ["mode"], "optional": ["delay"]},
}

#: Highest risk an action can have. Shown next to every step.
ACTION_RISK: dict[str, str] = {
    "reply": RISK_LOW,
    "system_info": RISK_LOW,
    "list_processes": RISK_LOW,
    "open": RISK_LOW,
    "open_url": RISK_LOW,
    "screenshot": RISK_LOW,
    "window_screenshot": RISK_LOW,
    "notification": RISK_LOW,
    "read_file": RISK_LOW,
    "list_dir": RISK_LOW,
    "lock_screen": RISK_MEDIUM,
    "volume": RISK_MEDIUM,
    "clipboard_get": RISK_MEDIUM,
    "clipboard_set": RISK_MEDIUM,
    "write_file": RISK_MEDIUM,
    "find_files": RISK_MEDIUM,
    "send_file": RISK_MEDIUM,
    "receive_file": RISK_MEDIUM,
    "reminder": RISK_MEDIUM,
    "scheduled_task": RISK_HIGH,
    "memory_update": RISK_MEDIUM,
    "memory_read": RISK_MEDIUM,
    "briefing_schedule": RISK_MEDIUM,
    "briefing": RISK_MEDIUM,
    "job_cancel": RISK_MEDIUM, "jobs_read": RISK_MEDIUM, "audit_read": RISK_MEDIUM,
    "reminder_delivery": RISK_MEDIUM, "cloud_plan": RISK_MEDIUM,
    "powershell": RISK_HIGH,
    "kill_process": RISK_HIGH,
    "power": RISK_HIGH,
}

ACTION_TITLES: dict[str, str] = {
    "reply": "Answer only (no action)",
    "system_info": "System report",
    "list_processes": "List processes",
    "open": "Open app / file / folder",
    "open_url": "Open website",
    "screenshot": "Full screenshot",
    "window_screenshot": "Window screenshot",
    "lock_screen": "Lock the screen",
    "notification": "Desktop notification",
    "volume": "Change volume",
    "clipboard_get": "Read clipboard",
    "clipboard_set": "Write clipboard",
    "read_file": "Read file",
    "write_file": "Write file",
    "list_dir": "List folder",
    "find_files": "Search for files",
    "send_file": "Send file to Telegram",
    "receive_file": "Save file from Telegram",
    "reminder": "Set a reminder",
    "scheduled_task": "Schedule a command",
    "memory_update": "Update local memory",
    "memory_read": "Send saved notes",
    "briefing_schedule": "Schedule a daily briefing approval",
    "briefing": "Send your daily briefing",
    "reminder_delivery": "Send a reminder", "cloud_plan": "Send request to external planner",
    "job_cancel": "Cancel a scheduled job", "jobs_read": "Send scheduled jobs", "audit_read": "Send audit log",
    "powershell": "PowerShell command",
    "kill_process": "Kill process",
    "power": "Power off / restart / sleep",
}

ACTION_ICONS: dict[str, str] = {
    "reply": "💬",
    "system_info": "📊",
    "list_processes": "📋",
    "open": "📂",
    "open_url": "🌐",
    "screenshot": "📸",
    "window_screenshot": "🪟",
    "lock_screen": "🔒",
    "notification": "🔔",
    "volume": "🔊",
    "clipboard_get": "📋",
    "clipboard_set": "📝",
    "read_file": "📄",
    "write_file": "💾",
    "list_dir": "🗂️",
    "find_files": "🔍",
    "send_file": "📤",
    "receive_file": "📥",
    "reminder": "⏰",
    "scheduled_task": "🕒",
    "memory_update": "🧠", "memory_read": "📝", "briefing_schedule": "🗓️", "briefing": "☀️", "job_cancel": "🗑️", "jobs_read": "🗓️", "audit_read": "📋", "reminder_delivery": "⏰", "cloud_plan": "🌐",
    "powershell": "🖥️",
    "kill_process": "☠️",
    "power": "⏻",
}

#: Longest accepted value per param (characters). Default 1000.
PARAM_MAX_LEN: dict[str, int] = {
    "request": 4000, "context": 2000, "destination": 1000,
    "steps": 12000,
    "command": 2000,
    "content": 20000,
    "text": 8000,
    "url": 2000,
    "path": 1000,
    "target": 1000,
    "file_name": 255,
    "sections": 200,
    "when": 40,
    "name": 200,
    "title": 200,
    "root": 1000,
}


def risk_of(action: str) -> str:
    return ACTION_RISK.get(action, RISK_HIGH)


def overall_risk(actions: list[str]) -> str:
    """Highest risk among the given actions (plans are executed in full)."""
    worst = RISK_LOW
    for action in actions:
        if RISK_ORDER.get(risk_of(action), 2) > RISK_ORDER[worst]:
            worst = risk_of(action)
    return worst


def is_known_action(action: str) -> bool:
    return action in PARAM_SPEC
