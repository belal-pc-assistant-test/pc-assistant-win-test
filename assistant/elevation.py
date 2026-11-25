"""Refuse to run as Administrator (Round 8).

On Windows the check is ``shell32.IsUserAnAdmin`` from ``ctypes``. On other systems
it always answers False. There is deliberately no override flag, and nothing here
asks for, or bypasses, UAC.
"""
from __future__ import annotations

import os
from typing import Callable

ELEVATED_EXIT_CODE = 5

REFUSAL_TEXT = (
    "⛔ The assistant refused to start because it is running as Administrator.\n"
    "   Start it again from a normal (non-elevated) window. Nothing was run."
)


def _windows_check() -> bool | None:
    """shell32.IsUserAnAdmin. None when the call is not available."""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:
        return None


def is_administrator(*, is_windows: bool | None = None,
                     check: Callable[[], bool | None] | None = None) -> bool | None:
    """True when the process runs elevated, False when it does not, None when unknown.

    ``is_windows`` and ``check`` exist so tests can stub the platform and the call.
    An unknown answer is not treated as elevated; ``--check`` reports it instead.
    """
    if is_windows is None:
        is_windows = os.name == "nt"
    if not is_windows:
        return False
    return (check or _windows_check)()
