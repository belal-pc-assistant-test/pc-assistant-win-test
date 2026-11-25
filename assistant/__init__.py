"""Telegram PC Assistant — a local, confirmation-gated PC assistant.

Public modules:

* ``assistant.config``  – reads config.ini
* ``assistant.planner`` – Ollama / keyword planner producing action plans
* ``assistant.actions`` – executes approved steps on Windows
* ``assistant.audit``   – local rotating audit log
* ``assistant.bot``     – Telegram handlers, buttons, execution flow
"""

import sys

__version__ = "3.1.0"


def _force_utf8_stdio() -> None:
    """Reconfigure stdout/stderr to UTF-8 (unencodable chars become '�').

    Windows consoles and CI pipes default to a legacy code page with strict
    error handling, so emoji output (action results, status icons) crashed
    with UnicodeEncodeError. Every entry point — ``main.py``, the Telegram
    bot, the capture worker, the screen indicator and this test suite —
    imports this package, so the reconfigure happens before anything prints.
    Text mode only: the capture worker keeps writing raw JPEG to
    ``sys.stdout.buffer``.
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None or not hasattr(stream, "reconfigure"):  # pythonw / redirects
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # stream already closed or detached
            pass


_force_utf8_stdio()
