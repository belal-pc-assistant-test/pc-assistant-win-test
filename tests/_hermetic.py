"""Test isolation for the PowerShell and admin-check test modules.

A test in these modules must never start a real process, open a network connection,
change the real token store, or leave global state behind for the next test.
``ModuleGuard`` makes any such call fail loudly with :class:`ForbiddenCall`, so a
missing stub shows up as a clear failure instead of a real action on the machine
that runs the tests (which may be a Windows CI runner).

Use it from ``setUpModule`` / ``tearDownModule``. Tests stub what they need with
their own ``mock.patch`` (the innermost patch wins). :class:`IsolatedCase` also
resets the per-test state.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import subprocess
import tempfile
import unittest
from typing import Any, Iterable
from unittest import mock

# Keep the real token store out of reach even if a test forgets to pass a path.
os.environ.setdefault("TPA_TOKEN_STORE_DIR", tempfile.mkdtemp(prefix="tpa-hermetic-store-"))
os.environ.pop("TELEGRAM_BOT_TOKEN", None)

from assistant import audit, token_store  # noqa: E402
from assistant.actions import base  # noqa: E402


class ForbiddenCall(AssertionError):
    """A test reached a real process, a network connection or another machine-changing call."""


# Every refused call is recorded here as well. Product code may catch the exception
# (a broad ``except Exception`` would hide it), so IsolatedCase fails the test from this list.
VIOLATIONS: list[str] = []


def _refuse(name: str):
    def refuse(*_args: Any, **_kwargs: Any) -> None:
        VIOLATIONS.append(name)
        raise ForbiddenCall(f"{name} was called for real. The test must stub it (mock.patch).")
    return refuse


# (object, attribute) pairs that must never run for real in a guarded module.
_FORBIDDEN: tuple[tuple[Any, str], ...] = (
    (subprocess, "Popen"),
    (subprocess, "run"),
    (subprocess, "call"),
    (subprocess, "check_call"),
    (subprocess, "check_output"),
    (os, "system"),
    (os, "startfile"),                       # Windows only; created for the guard on other systems
    (asyncio, "create_subprocess_exec"),
    (asyncio, "create_subprocess_shell"),
    (socket, "create_connection"),
    (asyncio.BaseEventLoop, "create_connection"),
)

# Outbound network: only loopback may be used (asyncio's own wake-up pipe on Windows is a loopback
# socketpair, and must keep working). Any other host is recorded and refused.
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
_ORIGINAL_CONNECT = socket.socket.connect
_ORIGINAL_GETADDRINFO = socket.getaddrinfo


def _is_loopback(host: object) -> bool:
    return isinstance(host, str) and host.lower() in _LOOPBACK


def _guarded_getaddrinfo(host: object, *args: Any, **kwargs: Any):
    if _is_loopback(host):
        return _ORIGINAL_GETADDRINFO(host, *args, **kwargs)
    return _refuse("socket.getaddrinfo (a host name lookup)")(host, *args, **kwargs)


def _guarded_connect(sock: socket.socket, address: Any) -> None:
    host = address[0] if isinstance(address, tuple) and address else None
    if _is_loopback(host):
        return _ORIGINAL_CONNECT(sock, address)
    return _refuse("socket connect to a non-loopback address")()


class ModuleGuard:
    """Blocks real process and network calls, and snapshots global state, for one module."""

    def __init__(self, extra: Iterable[tuple[Any, str, Any]] = ()) -> None:
        self._extra = tuple(extra)
        self._stack: contextlib.ExitStack | None = None
        self._secrets: set[str] = set()

    def start(self) -> None:
        self._stack = contextlib.ExitStack()
        self._secrets = set(audit._secrets)  # restored in stop(): registered secrets are global
        for target, name in _FORBIDDEN:
            self._stack.enter_context(mock.patch.object(target, name, _refuse(name), create=True))
        self._stack.enter_context(mock.patch.object(socket, "getaddrinfo", _guarded_getaddrinfo))
        self._stack.enter_context(mock.patch.object(socket.socket, "connect", _guarded_connect))
        for target, name, replacement in self._extra:
            self._stack.enter_context(mock.patch.object(target, name, replacement))

    def stop(self) -> None:
        if self._stack is not None:
            self._stack.close()
            self._stack = None
        audit._secrets.clear()
        audit._secrets.update(self._secrets)


class IsolatedCase(unittest.TestCase):
    """Per-test reset of the global state that one test could leave for the next."""

    def setUp(self) -> None:
        super().setUp()
        seen_before = len(VIOLATIONS)

        def no_forbidden_calls() -> None:
            new = VIOLATIONS[seen_before:]
            if new:
                raise AssertionError(f"a real call was attempted and must be stubbed: {', '.join(new)}")

        self.addCleanup(no_forbidden_calls)  # runs even when a subclass overrides tearDown
        saved_secrets = set(audit._secrets)
        self.addCleanup(lambda: (audit._secrets.clear(), audit._secrets.update(saved_secrets)))
        saved_error = token_store._last_powershell_error
        token_store._last_powershell_error = ""
        self.addCleanup(setattr, token_store, "_last_powershell_error", saved_error)
        base.clear_abort()
        self.addCleanup(base.clear_abort)
