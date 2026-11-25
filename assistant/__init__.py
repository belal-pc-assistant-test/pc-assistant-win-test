"""Telegram PC Assistant — a local, confirmation-gated PC assistant.

Public modules:

* ``assistant.config``  – reads config.ini
* ``assistant.planner`` – Ollama / keyword planner producing action plans
* ``assistant.actions`` – executes approved steps on Windows
* ``assistant.audit``   – local rotating audit log
* ``assistant.bot``     – Telegram handlers, buttons, execution flow
"""

__version__ = "3.1.0"
