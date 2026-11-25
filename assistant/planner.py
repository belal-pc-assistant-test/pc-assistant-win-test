"""Turn a free-text request (Arabic or English) into a concrete action plan.

Three planners, tried in the order set by ``[planner] mode`` in config.ini:

1. **cloud** – Pollinations' free, key-less OpenAI-compatible endpoint
   (``https://text.pollinations.ai/openai``).  No account, no API key.
   ⚠️ the text of the request leaves the machine when this backend is used.
2. **local** – a small model served by Ollama on this PC. Fully offline.
3. **keywords** – deterministic Arabic/English pattern matching, always available.

Whatever produced the plan, the output goes through the same strict JSON schema
validation and path hygiene checks before the user ever sees it.
"""

from __future__ import annotations

import json
import math
import os
import platform
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Action vocabulary (single source of truth in assistant.action_spec)
# ---------------------------------------------------------------------------

from .action_spec import (                                       # noqa: E402
    ACTION_ICONS,
    ACTION_RISK,
    ACTION_TITLES,
    PARAM_MAX_LEN,
    PARAM_SPEC,
    RISK_ICONS,
    VALID_ACTIONS,
    overall_risk,
    risk_of,
)

__all__ = [
    "ACTION_ICONS", "ACTION_RISK", "ACTION_TITLES", "PARAM_SPEC", "RISK_ICONS",
    "VALID_ACTIONS",
    "CloudPlanner", "LocalPlanner", "Plan", "PlanValidationError", "Planner",
    "PlannerError", "Step", "build_plan", "normalize_plan_dict", "overall_risk",
    "plan_json_schema", "risk_of", "rule_based_plan", "sanitize_and_annotate",
]

SUMMARY_MAX_LEN = 400
DESCRIPTION_MAX_LEN = 300


class PlannerError(RuntimeError):
    """A planner backend could not produce a plan."""


class PlanValidationError(PlannerError):
    """The model answered, but the JSON did not match the plan schema."""


@dataclass
class Step:
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    warning: str = ""

    def target_text(self) -> str:
        """Human readable one-liner of what this step will touch."""
        action, p = self.action, self.params
        if action == "powershell":
            return str(p.get("command", ""))
        if action == "open":
            return str(p.get("target", ""))
        if action == "screenshot":
            return "primary screen → PNG"
        if action in {"read_file", "list_dir"}:
            return str(p.get("path") or ".")
        if action == "write_file":
            return f"{p.get('path', '')} ({len(str(p.get('content', '')))} chars)"
        if action == "reply":
            return ""
        return json.dumps(p, ensure_ascii=False)


#: How a plan came into existence (only the first three are model backends).
PLAN_SOURCES = ("cloud", "local", "keywords", "direct", "scheduled")


@dataclass
class Plan:
    summary: str
    steps: list[Step]
    planner: str = "keywords"       # see PLAN_SOURCES
    model: str = ""                 # model name, when a model was used
    note: str = ""                  # why a backend was skipped/fallback used

    @property
    def needs_action(self) -> bool:
        return any(s.action != "reply" for s in self.steps)

    @property
    def reply_text(self) -> str | None:
        if len(self.steps) == 1 and self.steps[0].action == "reply":
            return str(self.steps[0].params.get("text", ""))
        return None

    @property
    def risk(self) -> str:
        """Highest risk among the steps of this plan."""
        return overall_risk([step.action for step in self.steps])

    def label(self) -> str:
        """Short label shown to the user, e.g. 'cloud · openai'."""
        if self.planner == "keywords":
            return "keywords (offline)"
        if self.planner == "local":
            return f"local · {self.model} (Ollama)"
        if self.planner == "direct":
            return "direct request (no model)"
        if self.planner == "scheduled":
            return "scheduled job"
        return f"cloud · {self.model} (pollinations.ai)"


# ---------------------------------------------------------------------------
# JSON schema for a plan (strict)
# ---------------------------------------------------------------------------

def _param_schema(action: str) -> dict[str, Any]:
    spec = PARAM_SPEC[action]
    properties: dict[str, Any] = {}
    for name in spec["required"] + spec["optional"]:
        properties[name] = {
            "type": "string",
            "minLength": 1,
            "maxLength": PARAM_MAX_LEN.get(name, 1000),
        }
    return {
        "type": "object",
        "required": list(spec["required"]),
        "additionalProperties": False,
        "properties": properties,
    }


def plan_json_schema(max_steps: int = 6) -> dict[str, Any]:
    """JSON Schema (draft-07) that a plan must satisfy."""
    variants = []
    for action in VALID_ACTIONS:
        variants.append({
            "type": "object",
            "required": ["action", "params"],
            "additionalProperties": False,
            "properties": {
                "action": {"const": action},
                "params": _param_schema(action),
                "description": {"type": "string", "maxLength": DESCRIPTION_MAX_LEN},
            },
        })
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "AssistantPlan",
        "type": "object",
        "required": ["summary", "steps"],
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string", "minLength": 1, "maxLength": SUMMARY_MAX_LEN},
            "steps": {
                "type": "array",
                "minItems": 1,
                "maxItems": max_steps,
                "items": {"oneOf": variants},
            },
        },
    }


#: Schema shown in the prompt / docs (default step cap).
PLAN_JSON_SCHEMA = plan_json_schema(6)

try:  # optional second, independent gate
    import jsonschema as _jsonschema
except Exception:  # pragma: no cover - optional dependency
    _jsonschema = None

HAVE_JSONSCHEMA = _jsonschema is not None


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are the planning module of a personal Windows 11 PC assistant controlled
through Telegram. You receive ONE request from the PC's owner, written in Arabic or English,
and you answer with ONE JSON object describing what should happen on the PC. A human reviews
and approves every plan before it runs, so be precise, minimal and honest.

OUTPUT CONTRACT (nothing outside the JSON: no markdown, no comments, no prose)
{"summary": "<one short sentence in the user's language>",
 "steps": [{"action": "<action>", "params": {...}, "description": "<short, user's language>"}]}

- "steps" is 1 to 6 items, executed in order. Use the fewest steps that fully answer the
  request; chain steps only when the request really contains several tasks.
- "description" is one short clause, in the user's language.

ALLOWED ACTIONS — exact names, exact params, nothing else:
  "reply"            {"text": "<message>"}                        answer only, run nothing
  "system_info"      {"sections": "cpu,battery,disks,memory,uptime,ip,windows"}   (all optional)
  "list_processes"   {"sort": "cpu|memory|name", "limit": "25"}
  "kill_process"     {"name": "<exe name>"} or {"pid": "<number>"}
  "open"             {"target": "<app name | absolute file/folder path>"}
  "open_url"         {"url": "<https URL>"}
  "screenshot"       {}                                          full screen
  "window_screenshot"{"title": "<optional window title fragment>"}
  "lock_screen"      {}
  "notification"     {"title": "<optional>", "text": "<message>"}
  "volume"           {"mode": "mute|unmute|toggle|up|down|set", "level": "0-100", "steps": "5"}
  "clipboard_get"    {}
  "clipboard_set"    {"text": "<text to copy>"}
  "read_file"        {"path": "<absolute path of an existing text file>"}
  "write_file"       {"path": "<absolute path>", "content": "<exact full text>"}
  "list_dir"         {"path": "<absolute folder path>"}
  "find_files"       {"root": "<absolute folder>", "name": "<fragment or glob>",
                      "ext": "<e.g. pdf>", "days": "<modified within N days>", "limit": "50"}
  "send_file"        {"path": "<absolute path of a file to send to Telegram>"}
  "receive_file"     {"file_id": "<telegram file id>", "file_name": "<name>"}   ONLY when the
                     user just uploaded that file in the same conversation
  "reminder"         {"text": "<what to remind>", "when": "+10m | 18:30 | 2026-01-31 18:30"}
  "scheduled_task"   {"when": "...", "command": "<PowerShell command>", "repeat": "once|daily"}
  "powershell"       {"command": "<one non-interactive PowerShell command>"}
  "power"            {"mode": "shutdown|restart|sleep|hibernate|cancel", "delay": "60"}

NON-NEGOTIABLE RULES
R1. NEVER invent a path, folder, file name, URL, app name or user name. Use only what the
    user actually wrote. If something needed is missing, return ONE "reply" step that asks
    for it in a single short question. Inventing a path is the worst possible mistake.
R2. Never emit placeholders: no C:\\path\\to\\file.txt, no <path>, no %USERPROFILE%\\...,
    no "..." , no /path/to. Either you have the real value or you ask (R1).
R3. If the request is ambiguous, incomplete, or has several plausible readings, ASK before
    acting: return exactly ONE "reply" step with a short question that offers the options you
    considered. Guessing is worse than asking.
R4. If the request is general knowledge, chit-chat, or impossible from a PC, return exactly
    ONE "reply" step and nothing else.
R5. Chain steps only for a genuinely multi-part request (e.g. "screenshot then tell me the
    free space" → screenshot + powershell). Never repeat a step, never exceed 6.
R6. Commands must be non-interactive and safe to run unattended: no prompts, no Read-Host,
    no interactive programs. Add flags such as -NoProfile / -Force / -Confirm:$false.
R7. Prefer read-only actions. Do not delete, format, partition, change passwords, edit the
    registry broadly, disable security tools, or kill system processes unless the user asked
    for exactly that. Prefer "power" over raw shutdown commands, "kill_process" over
    Stop-Process, and "volume"/"lock_screen"/"clipboard_*" over scripts.
R8. Never put secrets, tokens, passwords or API keys in the plan.
R9. Windows syntax only: drive letters (C:\\Users\\Me\\Desktop) or UNC (\\\\server\\share).
R10. Write "summary", "description", "reply.text" and "notification.text" in the SAME language
     as the request (Arabic request → Arabic text).
R11. Output must be valid JSON for a strict validator: no trailing commas, no comments, no
     code fences, no text before or after.
"""

#: Few-shot dialogue. Shows the model the exact shape plus the "ask, don't invent" rule.
FEW_SHOT: list[tuple[str, dict[str, Any]]] = [
    (
        "take a screenshot of my screen",
        {
            "summary": "Take a screenshot and send it back",
            "steps": [{"action": "screenshot", "params": {},
                       "description": "capture the screen"}],
        },
    ),
    (
        "خد لقطة شاشة وابعتلي اياها",
        {
            "summary": "أخذ لقطة شاشة وإرسالها",
            "steps": [{"action": "screenshot", "params": {},
                       "description": "تصوير الشاشة"}],
        },
    ),
    (
        "اقرأ الملف C:\\Users\\Me\\Desktop\\notes.txt",
        {
            "summary": "قراءة ملف الملاحظات",
            "steps": [{"action": "read_file",
                       "params": {"path": "C:\\Users\\Me\\Desktop\\notes.txt"},
                       "description": "قراءة الملف"}],
        },
    ),
    (
        "read my notes file",
        {
            "summary": "I need the full path of that file first",
            "steps": [{"action": "reply",
                       "params": {"text": "What is the full path of the notes file? "
                                          "For example C:\\Users\\Me\\Desktop\\notes.txt"},
                       "description": "ask for the missing path"}],
        },
    ),
    (
        "اعرض الملفات في C:\\Users\\Me\\Downloads",
        {
            "summary": "عرض محتويات مجلد التنزيلات",
            "steps": [{"action": "list_dir",
                       "params": {"path": "C:\\Users\\Me\\Downloads"},
                       "description": "عرض المجلد"}],
        },
    ),
    (
        "how much free space is left on drive C?",
        {
            "summary": "Check free space on drive C",
            "steps": [{"action": "powershell",
                       "params": {"command": "Get-PSDrive C | Select-Object Used,Free"},
                       "description": "read drive usage"}],
        },
    ),
    (
        "write \"buy milk\" into C:\\Users\\Me\\Desktop\\todo.txt",
        {
            "summary": "Write the note to todo.txt",
            "steps": [{"action": "write_file",
                       "params": {"path": "C:\\Users\\Me\\Desktop\\todo.txt",
                                  "content": "buy milk"},
                       "description": "write the file"}],
        },
    ),
    (
        "do the thing we talked about",
        {
            "summary": "The request is too vague to act on",
            "steps": [{"action": "reply",
                       "params": {"text": "Which task do you mean? Tell me what to do "
                                          "on the PC and I will show you a plan first."},
                       "description": "ask for clarification"}],
        },
    ),
    (
        "how is my battery?",
        {
            "summary": "Report the battery state",
            "steps": [{"action": "system_info", "params": {"sections": "battery"},
                       "description": "read the battery level"}],
        },
    ),
    (
        "اعرض ملفات PDF في C:\\Users\\Me\\Documents",
        {
            "summary": "البحث عن ملفات PDF في مجلد المستندات",
            "steps": [{"action": "find_files",
                       "params": {"root": "C:\\Users\\Me\\Documents", "ext": "pdf"},
                       "description": "البحث في المجلد"}],
        },
    ),
    (
        "remind me to call mom in 30 minutes",
        {
            "summary": "Set a reminder for 30 minutes from now",
            "steps": [{"action": "reminder",
                       "params": {"text": "call mom", "when": "+30m"},
                       "description": "create the reminder"}],
        },
    ),
    (
        "close chrome",
        {
            "summary": "Kill the chrome process",
            "steps": [{"action": "kill_process", "params": {"name": "chrome"},
                       "description": "stop chrome"}],
        },
    ),
    (
        "screenshot of the browser window then tell me the free space on C",
        {
            "summary": "Capture the browser window, then check free space",
            "steps": [
                {"action": "window_screenshot", "params": {"title": "chrome"},
                 "description": "capture the browser window"},
                {"action": "powershell",
                 "params": {"command": "Get-PSDrive C | Select-Object Used,Free"},
                 "description": "read drive usage"},
            ],
        },
    ),
    (
        "what is on my clipboard?",
        {
            "summary": "Read the clipboard",
            "steps": [{"action": "clipboard_get", "params": {},
                       "description": "read clipboard text"}],
        },
    ),
]


def build_messages(request: str, context_lines: list[str] | None = None) -> list[dict[str, str]]:
    """System prompt + few-shot examples + the actual request."""
    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for user_text, plan_obj in FEW_SHOT:
        messages.append({"role": "user", "content": user_text})
        messages.append({
            "role": "assistant",
            "content": json.dumps(plan_obj, ensure_ascii=False),
        })
    header = "\n".join(context_lines or [])
    messages.append({
        "role": "user",
        "content": (header + "\n\n" if header else "") + f"User request:\n{request}",
    })
    return messages


def environment_context(workspace_root: Path) -> list[str]:
    """Facts about this machine, useful for planning."""
    return [
        f"Current local date/time: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Operating system: {platform.system()} {platform.release()}",
        f"User home folder: {Path.home()}",
        f"Working folder: {workspace_root}",
        "Planner: return only the JSON plan.",
    ]


# ---------------------------------------------------------------------------
# Parsing, normalising and validating model output
# ---------------------------------------------------------------------------

ACTION_ALIASES = {
    # command-ish
    "run_command": "powershell", "command": "powershell", "cmd": "powershell",
    "shell": "powershell", "ps": "powershell", "terminal": "powershell",
    "execute": "powershell", "run": "powershell", "script": "powershell",
    # opening
    "open_app": "open", "open_file": "open", "launch": "open", "start": "open",
    "open_folder": "open", "open_website": "open_url", "browse": "open_url",
    "browser": "open_url", "url": "open_url", "goto": "open_url",
    # screenshots
    "screenshot_screen": "screenshot", "capture_screen": "screenshot",
    "screen": "screenshot", "screenshot_window": "window_screenshot",
    "capture_window": "window_screenshot", "window_capture": "window_screenshot",
    # information
    "sysinfo": "system_info", "systeminfo": "system_info", "sys_info": "system_info",
    "system_report": "system_info", "battery": "system_info", "hardware": "system_info",
    "processes": "list_processes", "list_process": "list_processes",
    "tasklist": "list_processes", "top": "list_processes",
    # processes / power
    "kill": "kill_process", "stop_process": "kill_process", "terminate": "kill_process",
    "close_app": "kill_process", "kill_app": "kill_process",
    "shutdown": "power", "reboot": "power", "restart": "power", "power_off": "power",
    "sleep": "power", "hibernate": "power", "poweroff": "power",
    # desktop
    "lock": "lock_screen", "lock_workstation": "lock_screen", "lock_pc": "lock_screen",
    "set_volume": "volume", "sound": "volume", "mute": "volume", "audio": "volume",
    "get_clipboard": "clipboard_get", "clipboard": "clipboard_get", "paste": "clipboard_get",
    "set_clipboard": "clipboard_set", "copy": "clipboard_set",
    "notify": "notification", "toast": "notification", "show_notification": "notification",
    # files
    "read": "read_file", "readfile": "read_file", "cat": "read_file",
    "write": "write_file", "writefile": "write_file", "save_file": "write_file",
    "create_file": "write_file",
    "list": "list_dir", "ls": "list_dir", "dir": "list_dir", "listdir": "list_dir",
    "search_files": "find_files", "find": "find_files", "locate": "find_files",
    "upload_file": "send_file", "export_file": "send_file", "attach": "send_file",
    "download_file": "receive_file", "save_upload": "receive_file",
    "receive": "receive_file",
    # automation
    "remind": "reminder", "alarm": "reminder", "remind_me": "reminder",
    "schedule": "scheduled_task", "cron": "scheduled_task", "at_time": "scheduled_task",
    # answers
    "answer": "reply", "say": "reply", "message": "reply", "ask": "reply",
}

#: Per-action corrections applied after the global aliases below.
ACTION_PARAM_FIXES: dict[str, dict[str, str]] = {
    "open_url": {"target": "url", "path": "url", "link": "url", "address": "url"},
    "clipboard_set": {"target": "text", "content": "text", "value": "text", "body": "text"},
    "notification": {"message": "text", "body": "text", "content": "text"},
    "reminder": {"message": "text", "content": "text", "at": "when", "time": "when",
                 "date": "when", "text_when": "when"},
    "scheduled_task": {"at": "when", "time": "when", "cmd": "command",
                       "script": "command", "schedule": "when", "message": "name"},
    "list_processes": {"top": "limit", "count": "limit", "order": "sort",
                       "order_by": "sort"},
    "kill_process": {"process": "name", "process_name": "name", "app": "name",
                     "process_id": "pid", "id": "pid"},
    "volume": {"value": "level", "volume": "level", "action": "mode"},
    "power": {"action": "mode", "command": "mode", "seconds": "delay", "time": "delay"},
    "find_files": {"pattern": "name", "query": "name", "extension": "ext",
                   "file_type": "ext", "path": "root", "folder": "root",
                   "directory": "root", "modified_days": "days"},
    "receive_file": {"file": "file_name", "filename": "file_name"},
    "system_info": {"section": "sections", "only": "sections"},
    "window_screenshot": {"window": "title", "app": "title"},
    "send_file": {"file": "path", "filename": "path"},
}

PARAM_ALIASES = {
    "cmd": "command", "command_line": "command", "script": "command", "powershell": "command",
    "app": "target", "application": "target", "url": "target", "file": "path",
    "filename": "path", "filepath": "path", "file_path": "path", "directory": "path",
    "folder": "path", "dir": "path", "location": "path", "path_to_open": "target",
    "body": "content", "data": "content", "text_content": "content",
    "answer": "text", "message": "text", "response": "text",
}


def extract_json_object(raw: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model reply."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise PlanValidationError(
                f"the reply contained no JSON object (got: {text[:120]!r})"
            ) from None
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise PlanValidationError(f"the reply contained invalid JSON ({exc})") from None
        except RecursionError:
            raise PlanValidationError("the reply is nested too deeply to be a plan") from None
    except RecursionError:
        raise PlanValidationError("the reply is nested too deeply to be a plan") from None

    if isinstance(data, list):
        data = {"summary": "", "steps": data}
    if not isinstance(data, dict):
        raise PlanValidationError("the reply JSON was not an object")
    return data


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def normalize_plan_dict(data: dict[str, Any], max_steps: int) -> dict[str, Any]:
    """Best-effort repair of common model quirks, then hand over to the validator.

    Keeps only known actions/params; maps aliases; coerces scalars to text;
    caps the step count. Everything that survives is then validated strictly.
    """
    if "steps" not in data and "action" in data:
        data = {"summary": data.get("summary", ""), "steps": [data]}

    raw_steps = data.get("steps")
    if isinstance(raw_steps, dict):
        raw_steps = [raw_steps]
    if not isinstance(raw_steps, list):
        raise PlanValidationError("the plan has no 'steps' list")

    normalized_steps: list[dict[str, Any]] = []
    for item in raw_steps:
        if not isinstance(item, dict):
            continue

        action = _as_text(item.get("action")).lower().replace("-", "_").replace(" ", "_")
        action = ACTION_ALIASES.get(action, action)
        if action not in VALID_ACTIONS:
            continue

        params: dict[str, Any] = {}
        raw_params = item.get("params")
        if isinstance(raw_params, dict):
            params.update(raw_params)
        # Some models put params at the top level instead of in "params".
        for key, value in item.items():
            if key in {"action", "params", "description", "reasoning", "thought"}:
                continue
            params.setdefault(key, value)

        allowed = set(PARAM_SPEC[action]["required"] + PARAM_SPEC[action]["optional"])
        fixes = ACTION_PARAM_FIXES.get(action, {})

        cleaned: dict[str, str] = {}
        for key, value in params.items():
            lowered = key.lower()
            canonical = fixes.get(lowered) or PARAM_ALIASES.get(lowered, lowered)
            if canonical not in allowed:
                # Keep the global alias only if the action accepts it.
                alternate = PARAM_ALIASES.get(lowered, lowered)
                canonical = alternate if alternate in allowed else canonical
            text = _as_text(value)
            if text and canonical not in cleaned:
                cleaned[canonical] = text

        # Cross-action conveniences.
        if action == "reply" and "text" not in cleaned and "content" in cleaned:
            cleaned["text"] = cleaned["content"]
        if action == "write_file" and "content" not in cleaned and "text" in cleaned:
            cleaned["content"] = cleaned["text"]
        if action == "clipboard_set" and "text" not in cleaned and "content" in cleaned:
            cleaned["text"] = cleaned["content"]
        if action in {"open_url", "open"} and action == "open_url" and "url" not in cleaned:
            if "target" in cleaned:
                cleaned["url"] = cleaned["target"]

        cleaned = {k: v for k, v in cleaned.items() if k in allowed}

        step: dict[str, Any] = {"action": action, "params": cleaned}
        description = _as_text(item.get("description"))[:DESCRIPTION_MAX_LEN]
        if description:
            step["description"] = description
        normalized_steps.append(step)

    if not normalized_steps:
        raise PlanValidationError("the plan contained no usable action")

    normalized_steps = normalized_steps[:max_steps]
    summary = _as_text(data.get("summary"))[:SUMMARY_MAX_LEN]
    if not summary:
        first = normalized_steps[0]
        summary = first.get("description") or f"Plan: {first['action']}"
    return {"summary": summary or "Plan", "steps": normalized_steps}


def _check_text_encodable(plan: dict[str, Any]) -> None:
    """Refuse lone surrogates (JSON ``\\ud800``): they cannot be sent to Telegram or hashed."""
    def walk(value: Any) -> None:
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                raise PlanValidationError("the plan contains text that is not valid Unicode") from None
        elif isinstance(value, dict):
            for key, item in value.items():
                walk(key)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(plan)


def _validate_builtin(plan: dict[str, Any], max_steps: int) -> None:
    """Strict structural checks (always run, no dependencies)."""
    if not isinstance(plan, dict):
        raise PlanValidationError("plan is not an object")
    extra = set(plan) - {"summary", "steps"}
    if extra:
        raise PlanValidationError(f"unexpected top-level keys: {sorted(extra)}")

    summary = plan.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise PlanValidationError("'summary' must be a non-empty string")
    if len(summary) > SUMMARY_MAX_LEN:
        raise PlanValidationError("'summary' is too long")

    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise PlanValidationError("'steps' must be a non-empty array")
    if len(steps) > max_steps:
        raise PlanValidationError(f"too many steps ({len(steps)} > {max_steps})")

    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise PlanValidationError(f"step {index} is not an object")
        step_extra = set(step) - {"action", "params", "description"}
        if step_extra:
            raise PlanValidationError(f"step {index} has unexpected keys: {sorted(step_extra)}")

        action = step.get("action")
        if action not in VALID_ACTIONS:
            raise PlanValidationError(f"step {index}: unknown action {action!r}")

        params = step.get("params")
        if not isinstance(params, dict):
            raise PlanValidationError(f"step {index}: 'params' must be an object")

        spec = PARAM_SPEC[action]
        allowed = set(spec["required"] + spec["optional"])
        unknown = set(params) - allowed
        if unknown:
            raise PlanValidationError(
                f"step {index} ({action}): unexpected params {sorted(unknown)}"
            )
        for name in spec["required"]:
            value = params.get(name)
            if not isinstance(value, str) or not value.strip():
                raise PlanValidationError(
                    f"step {index} ({action}): missing or empty '{name}'"
                )
        for name, value in params.items():
            if not isinstance(value, str):
                raise PlanValidationError(
                    f"step {index} ({action}): param '{name}' must be a string"
                )
            limit = PARAM_MAX_LEN.get(name, 1000)
            if len(value) > limit:
                raise PlanValidationError(
                    f"step {index} ({action}): param '{name}' is too long"
                )
            if name not in spec["required"] and not value.strip():
                raise PlanValidationError(
                    f"step {index} ({action}): param '{name}' is empty"
                )

        description = step.get("description")
        if description is not None and not isinstance(description, str):
            raise PlanValidationError(f"step {index}: 'description' must be a string")


def _validate_jsonschema(plan: dict[str, Any], max_steps: int) -> None:
    """Second gate using the jsonschema library when it is installed."""
    if not HAVE_JSONSCHEMA:
        return
    try:
        _jsonschema.validate(instance=plan, schema=plan_json_schema(max_steps))
    except Exception as exc:  # jsonschema.ValidationError / SchemaError
        raise PlanValidationError(f"schema validation failed: {exc}") from None


# ---------------------------------------------------------------------------
# Path hygiene: the model must never invent paths
# ---------------------------------------------------------------------------

PLACEHOLDER_MARKERS = (
    "<", ">", "{", "}", "...", "…", "/path/to", "\\path\\to", "path/to", "path\\to",
    "your_", "your-", "yourfile", "%userprofile%\\path", "$path", "$file",
    "\\path\\file", "/path/file", "example.com/file", "xxxx", "placeholder",
)

PATH_PARAM_ACTIONS = {
    "read_file": "path",
    "write_file": "path",
    "list_dir": "path",
    "screenshot": "path",
    "window_screenshot": "path",
    "send_file": "path",
    "find_files": "root",
    "receive_file": "target",
}

#: Actions whose path parameter must exist before the plan makes sense.
EXISTING_PATH_ACTIONS = {"read_file", "list_dir", "send_file", "find_files"}


def looks_like_path(text: str) -> bool:
    t = (text or "").strip()
    return bool(
        re.match(r"^[a-zA-Z]:[\\/]", t)            # C:\...
        or t.startswith(("\\\\", "~/", ".\\", "..\\", "./", "../"))
        or ("\\" in t or "/" in t)
        or re.search(r"\.\w{1,5}$", t)
    )


def is_placeholder_value(value: str) -> bool:
    lowered = (value or "").lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


WINDOWS_PATH_RE = re.compile(r"^([a-zA-Z]:[\\/]|" + re.escape("\\\\") + ")")  # drive, or two-backslash UNC


def _resolve_light(raw: str, workspace_root: Path) -> Path:
    """Resolve a path the same way the executor would (no allow-list check)."""
    cleaned = (raw or "").strip().strip('"').strip("'")
    cleaned = os.path.expandvars(os.path.expanduser(cleaned))
    path = Path(cleaned)
    # A Windows path stays absolute even when this check runs on POSIX (tests/CI).
    if not path.is_absolute() and not WINDOWS_PATH_RE.match(cleaned):
        path = workspace_root / path
    return path


def _is_network_or_device_text(text: str) -> bool:
    """Same test on the text the model produced, before any join with the workspace."""
    normalized = (text or "").strip().strip('"').strip("'").replace("/", "\\")
    return normalized.startswith("\\\\") or normalized.startswith("\\??\\")


def _is_network_or_device(path: Path) -> bool:
    """UNC (\\\\server\\share), \\\\?\\ and \\\\.\\ device, and \\??\\ NT paths.

    On Windows, even an existence check on these can contact a remote host and
    send credentials, so the planner must not probe them. The executor refuses
    them anyway (see ``base.check_path_syntax``).
    """
    text = str(path).replace("/", "\\")
    return text.startswith("\\\\") or text.startswith("\\??\\")


def sanitize_and_annotate(
    steps: list[Step], workspace_root: Path
) -> tuple[list[Step], list[str]]:
    """Drop placeholder paths, warn about paths that do not exist on this PC.

    Returns the surviving steps plus human readable notes about what was dropped.
    """
    kept: list[Step] = []
    notes: list[str] = []

    for step in steps:
        param_name = PATH_PARAM_ACTIONS.get(step.action)
        path_value = ""
        if param_name:
            path_value = str(step.params.get(param_name, "") or "")
        elif step.action == "open":
            candidate = str(step.params.get("target", "") or "")
            if looks_like_path(candidate) and not candidate.lower().startswith(
                ("http://", "https://", "www.", "mailto:")
            ):
                path_value = candidate
                param_name = "target"

        if path_value and is_placeholder_value(path_value):
            notes.append(
                f"dropped a step with a placeholder path ({path_value!r}) — "
                "the assistant must not invent paths"
            )
            continue

        if path_value:
            resolved = _resolve_light(path_value, workspace_root)
            if _is_network_or_device_text(path_value) or _is_network_or_device(resolved):
                # Keep the step so the executor refuses it with a clear message, but
                # never probe the path here.
                step.warning = "⚠️ network and device paths are refused."
                kept.append(step)
                continue
            exists = resolved.exists()
            if step.action in EXISTING_PATH_ACTIONS and not exists:
                step.warning = f"⚠️ does not exist on this PC: {resolved}"
            elif step.action == "open" and not exists:
                step.warning = f"⚠️ path not found (Windows may still open it): {resolved}"
            elif step.action == "write_file" and not resolved.parent.exists():
                step.warning = f"ℹ️ the folder will be created: {resolved.parent}"
        elif step.action == "read_file" and not str(step.params.get("path", "")).strip():
            continue

        kept.append(step)

    if not kept:
        kept = [Step(
            "reply",
            {"text": (
                "I could not turn that into a plan I trust — I would have had to guess a "
                "file path, which I never do. Please resend the request with the full "
                "path, e.g. C:\\Users\\Me\\Desktop\\notes.txt\n"
                "لم أتمكن من تكوين خطة موثوقة لأن المسار غير معروف. أعد إرسال الطلب "
                "مع المسار الكامل."
            )},
            "ask for the real path",
        )]
    return kept, notes


def build_plan(
    raw_content: str,
    planner: str,
    model: str,
    max_steps: int,
    workspace_root: Path | None = None,
) -> Plan:
    """Model text → validated :class:`Plan`.

    Anything that is not a well-formed plan raises :class:`PlanValidationError`,
    never another exception, so the planner chain can fall back cleanly.
    """
    try:
        data = extract_json_object(raw_content)
        normalized = normalize_plan_dict(data, max_steps)
    except PlanValidationError:
        raise
    except (RecursionError, ValueError, TypeError, OverflowError, KeyError, AttributeError) as exc:
        raise PlanValidationError(f"the reply could not be read as a plan ({type(exc).__name__})") from None
    _check_text_encodable(normalized)
    _validate_builtin(normalized, max_steps)
    _validate_jsonschema(normalized, max_steps)

    steps = [
        Step(
            action=step["action"],
            params=dict(step["params"]),
            description=step.get("description", ""),
        )
        for step in normalized["steps"]
    ]

    notes: list[str] = []
    if workspace_root is not None:
        steps, notes = sanitize_and_annotate(steps, workspace_root)

    return Plan(
        summary=normalized["summary"],
        steps=steps,
        planner=planner,
        model=model,
        note="; ".join(notes),
    )


# ---------------------------------------------------------------------------
# Backend 1: Pollinations (free, key-less, cloud)
# ---------------------------------------------------------------------------

class CloudPlanner:
    """OpenAI-compatible endpoint at text.pollinations.ai — no API key."""

    def __init__(
        self,
        url: str = "https://text.pollinations.ai/openai",
        model: str = "openai",
        timeout: float = 45.0,
        use_response_format: bool = True,
        cooldown_seconds: int = 120,
    ) -> None:
        self.url = url
        self.model = model
        self.timeout = timeout
        self.use_response_format = use_response_format
        self.cooldown_seconds = max(10, int(cooldown_seconds))
        self._cooldown_until = 0.0
        self.last_error = ""

    # -- state ----------------------------------------------------------
    def cooling_down(self) -> tuple[bool, str]:
        remaining = self._cooldown_until - time.time()
        if remaining > 0:
            return True, f"rate-limited, retrying in {int(remaining)}s"
        return False, ""

    def reset_cooldown(self) -> None:
        self._cooldown_until = 0.0

    # -- planning -------------------------------------------------------
    def plan(self, request: str, workspace_root: Path, max_steps: int = 6) -> Plan:
        cooling, why = self.cooling_down()
        if cooling:
            raise PlannerError(f"cloud planner paused ({why})")

        messages = build_messages(request, environment_context(workspace_root))
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
        }
        if self.use_response_format:
            body["response_format"] = {"type": "json_object"}

        payload = self._post(body)
        content = self._extract_content(payload)
        return build_plan(content, "cloud", self.model, max_steps, workspace_root)

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        import requests

        try:
            resp = requests.post(
                self.url,
                json=body,
                timeout=self.timeout,
                headers={"Accept": "application/json"},
                proxies={"http": "", "https": ""},
                allow_redirects=False,
            )
        except requests.exceptions.Timeout:
            raise PlannerError(
                f"cloud planner timed out after {self.timeout:.0f}s"
            ) from None
        except requests.exceptions.ConnectionError:
            raise PlannerError("cloud planner unreachable (no internet?)") from None
        except Exception as exc:
            raise PlannerError(f"cloud planner request failed: {exc}") from None

        if resp.status_code in (402, 429):
            # 429 = classic rate limit, 402 = Pollinations' free-tier allowance
            # ("Free limit reached: 1 reply every 45 seconds per IP").
            wait = self._retry_hint(resp) or self.cooldown_seconds
            wait = max(15, min(int(wait), 600))
            self._cooldown_until = time.time() + wait
            reason = "free-tier limit" if resp.status_code == 402 else "rate limited"
            raise PlannerError(
                f"cloud planner {reason} (HTTP {resp.status_code}), paused for {wait}s"
            )

        if resp.status_code == 400 and "response_format" in body:
            # Some deployments do not support response_format — retry without it.
            retry_body = dict(body)
            retry_body.pop("response_format", None)
            return self._post(retry_body)

        if resp.status_code in (401, 403):
            raise PlannerError(f"cloud planner refused the request (HTTP {resp.status_code})")
        if resp.status_code >= 500:
            # Do not hammer a broken service: short pause, then try again later.
            self._cooldown_until = max(
                self._cooldown_until, time.time() + min(30, self.cooldown_seconds)
            )
            raise PlannerError(f"cloud planner is down (HTTP {resp.status_code})")
        if resp.status_code != 200:
            raise PlannerError(f"cloud planner returned HTTP {resp.status_code}")

        try:
            data = resp.json()
        except ValueError:
            snippet = (resp.text or "")[:120].replace("\n", " ")
            raise PlannerError(f"cloud planner returned non-JSON: {snippet!r}") from None
        if not isinstance(data, dict):
            raise PlannerError("cloud planner returned an unexpected payload")
        return data

    @staticmethod
    def _retry_hint(resp: Any) -> float | None:
        """Seconds to wait, from the Retry-After header or the error body."""
        raw = (resp.headers.get("Retry-After") or "").strip()
        if raw:
            try:
                value = float(raw)
            except ValueError:
                value = float("nan")
            if math.isfinite(value):   # "nan" / "inf" must not crash the int() below
                return value
        try:
            body = resp.json()
        except Exception:
            return None
        error = body.get("error") if isinstance(body, dict) else None
        message = ""
        if isinstance(error, dict):
            message = str(error.get("message", ""))
        elif isinstance(error, str):
            message = error
        match = re.search(r"retry after\s*(\d+)", message, re.IGNORECASE)
        if match:
            return float(match.group(1))
        return None

    @staticmethod
    def _extract_content(payload: dict[str, Any]) -> str:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            error = payload.get("error") if isinstance(payload.get("error"), dict) else None
            if error:
                raise PlannerError(f"cloud planner error: {error.get('message', 'unknown')}")
            raise PlannerError("cloud planner returned no choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            raise PlannerError("cloud planner returned a malformed choice")
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        if not isinstance(content, str) or not content.strip():
            raise PlannerError("cloud planner returned an empty answer")
        return content


# ---------------------------------------------------------------------------
# Backend 2: local Ollama
# ---------------------------------------------------------------------------

@dataclass
class OllamaStatus:
    reachable: bool
    detail: str
    models: list[str] = field(default_factory=list)


class LocalPlanner:
    """Small local model served by Ollama — fully offline, no key."""

    def __init__(self, base_url: str, model: str, timeout: float = 90.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def status(self) -> OllamaStatus:
        import requests

        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=5)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.ConnectionError:
            return OllamaStatus(False, f"Ollama is not running at {self.base_url}")
        except requests.exceptions.Timeout:
            return OllamaStatus(False, f"Ollama at {self.base_url} did not answer in 5s")
        except Exception as exc:
            return OllamaStatus(False, f"Ollama check failed: {exc}")

        models = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
        base = self.model.split(":")[0]
        has_model = any(
            m == self.model or m.startswith(base + ":") or m.split(":")[0] == base
            for m in models
        )
        if not models:
            return OllamaStatus(True, "Ollama is running but has no models pulled yet.", models)
        if not has_model:
            return OllamaStatus(
                True,
                f"Ollama is running but model '{self.model}' is not installed "
                f"(ollama pull {self.model})",
                models,
            )
        return OllamaStatus(True, f"Ollama ready with model '{self.model}'.", models)

    def plan(self, request: str, workspace_root: Path, max_steps: int = 6) -> Plan:
        import requests

        payload = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0, "num_predict": 900},
            "messages": build_messages(request, environment_context(workspace_root)),
        }
        try:
            # Do not forward local request content through configured proxies or
            # redirect it to an unapproved remote endpoint.
            resp = requests.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout,
                                 proxies={"http": "", "https": ""}, allow_redirects=False)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.ConnectTimeout:
            raise PlannerError(f"local model timed out at {self.base_url}") from None
        except requests.exceptions.ConnectionError:
            raise PlannerError(f"local model not running at {self.base_url}") from None
        except requests.exceptions.Timeout:
            raise PlannerError(f"local model took longer than {self.timeout:.0f}s") from None
        except requests.exceptions.HTTPError as exc:
            status = getattr(exc.response, "status_code", "?")
            hint = " (is the model pulled?)" if status == 404 else ""
            raise PlannerError(f"local model returned HTTP {status}{hint}") from None
        except ValueError:
            raise PlannerError("local model sent a non-JSON response") from None
        except Exception as exc:
            raise PlannerError(f"local model request failed: {exc}") from None

        content = (data.get("message") or {}).get("content", "")
        if not isinstance(content, str) or not content.strip():
            raise PlannerError("local model returned an empty answer")
        return build_plan(content, "local", self.model, max_steps, workspace_root)


# ---------------------------------------------------------------------------
# Backend 3: keyword/rule fallback (always available)
# ---------------------------------------------------------------------------

APP_ALIASES = {
    "chrome": "chrome", "google chrome": "chrome",
    "edge": "msedge", "firefox": "firefox",
    "notepad": "notepad", "calculator": "calc", "calc": "calc",
    "paint": "mspaint", "explorer": "explorer", "file explorer": "explorer",
    "files": "explorer", "terminal": "wt", "cmd": "cmd",
    "command prompt": "cmd", "settings": "ms-settings:", "task manager": "taskmgr",
    "word": "winword", "excel": "excel", "powerpoint": "powerpnt",
    "vscode": "code", "vs code": "code", "spotify": "spotify",
    "كروم": "chrome", "جوجل كروم": "chrome",
    "الحاسبة": "calc", "الآلة الحاسبة": "calc", "الاله الحاسبة": "calc",
    "المفكرة": "notepad", "الرسام": "mspaint",
    "مستكشف الملفات": "explorer", "الملفات": "explorer", "الطرفية": "wt",
    "الاعدادات": "ms-settings:", "الإعدادات": "ms-settings:",
    "مدير المهام": "taskmgr", "وورد": "winword", "اكسل": "excel", "إكسل": "excel",
}

RAW_COMMAND_STARTS = (
    "ipconfig", "ping", "dir", "ls", "tasklist", "taskkill", "systeminfo",
    "whoami", "hostname", "netstat", "nslookup", "tracert", "cls", "echo",
    "get-", "set-", "new-", "start-", "stop-", "restart-", "remove-", "copy-",
    "move-", "test-", "invoke-", "shutdown", "sfc", "chkdsk", "vol", "ver",
)

SCREENSHOT_KW = ["take a screenshot", "take screenshot", "screenshot", "screen shot",
                 "capture screen", "capture the screen", "لقطة شاشة", "لقطة للشاشة",
                 "خد لقطة", "خذ لقطة", "صورة للشاشة", "صور الشاشة", "سكرين شوت", "الشاشة"]
LIST_KW = ["list files", "list folder", "list dir", "list the folder", "list the directory",
           "show files", "show me the files", "what's in", "whats in", "folder content",
           "اعرض الملفات", "اعرض محتويات", "قائمة الملفات", "محتويات المجلد", "محتويات",
           "ايه اللي في", "ماذا يوجد في", "الملفات في", "قائمه الملفات"]
READ_KW = ["read file", "read the file", "show file content", "open and read", "read",
           "اقرأ الملف", "اقرا الملف", "اقرأ", "اقرا", "محتوى الملف", "contents of"]
WRITE_KW = ["write file", "write to", "save to", "create file", "create a file", "write",
            "اكتب في", "احفظ في", "أنشئ ملف", "انشئ ملف", "اكتب ملف"]
POWERSHELL_KW = ["run command", "run the command", "execute", "powershell", "shell command",
                 "run in terminal", "نفذ الأمر", "نفّذ الأمر", "نفذ", "نفّذ", "أمر",
                 "امر", "شغل الأمر", "شغّل الأمر", "باور شل"]
OPEN_KW = ["open ", "launch ", "start app", "run app", "go to ", "افتح ", "شغل ", "شغّل ",
           "ابدأ ", "اذهب الى ", "اذهب إلى "]


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _strip_wrapping_quotes(text: str) -> str:
    text = text.strip()
    for opener, closer in [('"', '"'), ("'", "'"), ("«", "»"), ("“", "”"), ("`", "`")]:
        if len(text) >= 2 and text.startswith(opener) and text.endswith(closer):
            return text[1:-1].strip()
    return text


def _strip_prefix(text: str, keywords: list[str]) -> str:
    low = text.lower()
    for kw in keywords:
        if low.startswith(kw):
            rest = text[len(kw):]
            return _strip_wrapping_quotes(rest.strip(" :,-—–\t\n"))
    return text.strip()


def _extract_write(text: str) -> tuple[str, str] | None:
    for separator in ("::", "<<<", "==>"):
        if separator in text:
            path_part, content = text.split(separator, 1)
            return path_part.strip(), content.lstrip("\n ")
    if "\n" in text:
        first, rest = text.split("\n", 1)
        if looks_like_path(first) and rest.strip():
            return first.strip(), rest.lstrip("\n ")
    return None


def rule_based_plan(request: str, workspace_root: Path | None = None) -> Plan:
    """Deterministic keyword planner (Arabic + English)."""
    text = _clean(request)
    ws = str(workspace_root) if workspace_root else "."
    if not text:
        return Plan(
            summary="Empty request.",
            steps=[Step("reply", {"text": "I received an empty message."})],
            planner="keywords",
        )

    low = text.lower()

    def keyword_plan(summary: str, steps: list[Step]) -> Plan:
        return Plan(summary=summary, steps=steps, planner="keywords")

    # 1) Explicit raw command escape hatch
    for prefix in ("!", "!ps ", "ps:", "باورشل:", "امر:", "أمر:"):
        if low.startswith(prefix):
            command = text[len(prefix):].strip()
            if command:
                return keyword_plan(
                    f"Run PowerShell command: {command[:120]}",
                    [Step("powershell", {"command": command}, "Run in PowerShell")],
                )

    # 2) Screenshot
    if any(kw in low for kw in SCREENSHOT_KW):
        return keyword_plan(
            "Take a screenshot of the screen and send it back.",
            [Step("screenshot", {}, "Capture the screen and return an approved JPEG photo")],
        )

    # 3) List folder
    if any(kw in low for kw in LIST_KW):
        target = ""
        for kw in sorted(LIST_KW, key=len, reverse=True):
            if kw in low:
                target = text[low.index(kw) + len(kw):].strip(" :")
                break
        target = _strip_wrapping_quotes(target) or ws
        return keyword_plan(
            f"List the folder: {target}",
            [Step("list_dir", {"path": target}, f"List {target}")],
        )

    # 4) Write a file
    if any(kw in low for kw in WRITE_KW):
        rest = text
        for kw in sorted(WRITE_KW, key=len, reverse=True):
            if kw in low:
                rest = text[low.index(kw) + len(kw):].strip(" :,-")
                break
        parsed = _extract_write(rest)
        if parsed:
            path, content = parsed
            return keyword_plan(
                f"Write {len(content)} characters to {path}",
                [Step("write_file", {"path": path, "content": content}, f"Write file {path}")],
            )
        return keyword_plan(
            "I need the path and the content to write a file.",
            [Step("reply", {"text": (
                "To write a file, send it like this:\n\n"
                "write C:\\Users\\me\\note.txt :: hello world\n\n"
                "or: اكتب في C:\\Users\\me\\note.txt :: مرحبا"
            )})],
        )

    # 5) Read a file
    if any(kw in low for kw in READ_KW):
        rest = text
        for kw in sorted(READ_KW, key=len, reverse=True):
            if low.startswith(kw) or kw in low:
                rest = text[low.index(kw) + len(kw):].strip(" :,-")
                break
        rest = _strip_wrapping_quotes(rest)
        if not rest:
            return keyword_plan(
                "Which file should I read?",
                [Step("reply", {"text": "Which file should I read? Send the full path."})],
            )
        return keyword_plan(
            f"Read the file: {rest}",
            [Step("read_file", {"path": rest}, f"Read {rest}")],
        )

    # 6) Raw-looking command
    first_word = re.split(r"[\s/]", low, maxsplit=1)[0]
    if first_word in RAW_COMMAND_STARTS or any(low.startswith(kw) for kw in POWERSHELL_KW):
        command = text
        for kw in sorted(POWERSHELL_KW, key=len, reverse=True):
            if low.startswith(kw):
                command = _strip_prefix(text, [kw])
                break
        return keyword_plan(
            f"Run PowerShell command: {command[:120]}",
            [Step("powershell", {"command": command}, "Run in PowerShell")],
        )

    # 7) Open an app / file / URL
    if any(low.startswith(kw) for kw in OPEN_KW) or any(kw in low for kw in OPEN_KW):
        rest = text
        for kw in sorted(OPEN_KW, key=len, reverse=True):
            if low.startswith(kw):
                rest = text[len(kw):].strip(" :,-")
                break
        rest = _strip_wrapping_quotes(rest)
        if not rest:
            return keyword_plan(
                "What should I open?",
                [Step("reply", {"text": "What should I open? Example: افتح كروم"})],
            )
        target = APP_ALIASES.get(rest.lower(), rest)
        return keyword_plan(
            f"Open: {target}",
            [Step("open", {"target": target}, f"Open {target}")],
        )

    # 8) Unknown → helpful reply, no action
    return keyword_plan(
        "No actionable instruction recognised (keyword fallback).",
        [Step("reply", {"text": (
            "I could not map that to an action with the offline keyword planner.\n\n"
            "Try one of these forms:\n"
            "• screenshot / لقطة شاشة\n"
            "• open chrome / افتح كروم\n"
            "• list files in C:\\Users\\me\\Desktop / اعرض الملفات في ...\n"
            "• read C:\\path\\to\\file.txt / اقرأ الملف ...\n"
            "• write C:\\path.txt :: content / اكتب في ... :: ...\n"
            "• !Get-Process / run ipconfig   (raw PowerShell, note the ! prefix)\n\n"
            "Tip: enable the cloud or Ollama planner in config.ini so I understand "
            "free-form requests too."
        )})],
    )


# ---------------------------------------------------------------------------
# Facade: the configured chain of backends
# ---------------------------------------------------------------------------

class Planner:
    """Runs the backends in order (cloud → local → keywords) and never raises."""

    def __init__(
        self,
        mode: str = "cloud",
        cloud: CloudPlanner | None = None,
        local: LocalPlanner | None = None,
        workspace_root: Path | None = None,
        max_steps: int = 6,
    ) -> None:
        if mode not in ("cloud", "local", "keywords"):
            raise ValueError(f"unknown planner mode: {mode}")
        self.mode = mode
        self.cloud = cloud
        self.local = local
        self.workspace_root = workspace_root or Path(".")
        self.max_steps = max_steps

    @classmethod
    def from_config(cls, config: Any) -> "Planner":
        cloud = CloudPlanner(
            url=config.cloud_url,
            model=config.cloud_model,
            timeout=config.cloud_timeout,
            use_response_format=config.cloud_response_format,
            cooldown_seconds=config.cloud_cooldown,
        )
        local = (
            LocalPlanner(
                base_url=config.ollama_base_url,
                model=config.ollama_model,
                timeout=config.ollama_timeout,
            )
            if config.use_ollama
            else None
        )
        return cls(
            mode=config.planner_mode,
            cloud=cloud,
            local=local,
            workspace_root=config.workspace_root,
            max_steps=config.max_plan_steps,
        )

    # -- chain ----------------------------------------------------------
    def chain(self) -> list[str]:
        if self.mode == "keywords":
            return ["keywords"]
        if self.mode == "local":
            return ["local", "keywords"] if self.local else ["keywords"]
        chain = ["cloud"]
        if self.local:
            chain.append("local")
        chain.append("keywords")
        return chain

    def plan(self, request: str) -> Plan:
        stripped = request.strip()

        # Explicit raw-command escape hatch never needs a model.
        if stripped.startswith(("!", "ps:")) or stripped.lower().startswith("!ps "):
            return self._finish(rule_based_plan(request, self.workspace_root), [])

        failures: list[str] = []
        for backend in self.chain():
            if backend == "cloud":
                if self.cloud is None:
                    continue
                try:
                    plan = self.cloud.plan(request, self.workspace_root, self.max_steps)
                    return self._finish(plan, failures)
                except PlannerError as exc:
                    failures.append(f"cloud: {exc}")
                    continue
                except Exception as exc:  # a bug in one backend must not stop the chain
                    failures.append(f"cloud: internal error ({type(exc).__name__})")
                    continue
            if backend == "local":
                if self.local is None:
                    continue
                try:
                    plan = self.local.plan(request, self.workspace_root, self.max_steps)
                    return self._finish(plan, failures)
                except PlannerError as exc:
                    failures.append(f"local: {exc}")
                    continue
                except Exception as exc:
                    failures.append(f"local: internal error ({type(exc).__name__})")
                    continue
            break  # keywords is the last resort, handled below

        plan = rule_based_plan(request, self.workspace_root)
        return self._finish(plan, failures)

    def _finish(self, plan: Plan, failures: list[str]) -> Plan:
        plan.steps = [s for s in plan.steps if s.action in VALID_ACTIONS][: self.max_steps]
        if not plan.steps:
            plan.steps = rule_based_plan("", self.workspace_root).steps

        steps, notes = sanitize_and_annotate(plan.steps, self.workspace_root)
        plan.steps = steps

        notes = list(notes)
        if failures:
            notes.insert(0, "planner fallback — " + "; ".join(failures))
        if plan.note:
            notes.append(plan.note)
        plan.note = "; ".join(n for n in notes if n)[:400]
        return plan

    # -- diagnostics ----------------------------------------------------
    def status(self) -> dict[str, Any]:
        info: dict[str, Any] = {"mode": self.mode, "chain": self.chain()}
        if self.cloud is not None:
            cooling, why = self.cloud.cooling_down()
            info["cloud"] = {
                "url": self.cloud.url,
                "model": self.cloud.model,
                "cooling_down": cooling,
                "detail": why or "ready",
            }
        if self.local is not None:
            status = self.local.status()
            info["local"] = {
                "url": self.local.base_url,
                "model": self.local.model,
                "reachable": status.reachable,
                "detail": status.detail,
                "models": status.models,
            }
        return info
