"""All actions, split by domain and dispatched through :data:`HANDLERS`.

Modules
-------
``base``      results, path resolution, subprocesses, /panic support
``core``      reply, powershell, open, open_url, screenshots, file read/write
``system``    system_info, processes, volume, clipboard, lock, notification, power
``transfer``  send_file, receive_file, find_files
``dispatch``  the action registry + ``execute_step``
"""

from .base import (
    IS_WINDOWS,
    REGISTRY,
    ActionContext,
    ActionError,
    ActionResult,
    Transport,
    abort_requested,
    clear_abort,
    effective_roots,
    ensure_inside,
    human_bytes,
    human_duration,
    request_abort,
    resolve_in_roots,
    resolve_path,
    run_process,
    run_powershell,
    sanitize_filename,
    truncate,
    unique_destination,
)
from .core import (
    list_dir,
    open_target,
    open_url,
    powershell,
    read_file,
    reply,
    take_screenshot,
    write_file,
)
from .automation import reminder, scheduled_task
from .dispatch import HANDLERS, execute_step, step_preview
from .system import (
    PROTECTED_PROCESSES,
    is_protected_process,
    kill_process,
    list_processes,
    lock_screen,
    notification,
    power,
    power_argv,
    render_system_info,
    system_info,
    volume,
    volume_key_sequence,
    clipboard_get,
    clipboard_set,
    window_screenshot,
)
from .transfer import find_files, receive_file, send_file

__all__ = [
    "HANDLERS", "PROTECTED_PROCESSES", "REGISTRY", "ActionContext", "ActionError",
    "ActionResult", "IS_WINDOWS", "Transport", "abort_requested", "clear_abort",
    "clipboard_get", "clipboard_set", "effective_roots", "ensure_inside",
    "execute_step", "find_files", "human_bytes", "human_duration",
    "is_protected_process", "kill_process", "list_dir", "list_processes",
    "lock_screen", "notification", "open_target", "open_url", "power",
    "power_argv", "powershell", "read_file", "receive_file",
    "render_system_info", "reminder", "reply", "request_abort", "resolve_in_roots",
    "resolve_path", "run_process", "run_powershell", "sanitize_filename",
    "scheduled_task", "send_file", "step_preview", "system_info",
    "take_screenshot", "truncate",
    "unique_destination", "volume", "volume_key_sequence", "window_screenshot",
    "write_file",
]
