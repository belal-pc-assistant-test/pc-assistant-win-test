"""Always-on-top red banner while a remote session is active.

The banner runs as a separate process (``python -m assistant.remote.banner``) so Tk
never shares a thread with the bot. It shows the time left, the viewer state, the
stop hotkey (Ctrl+Alt+Shift+Q, registered system-wide) and a Stop button.

Protocol, one line each:
    parent -> banner (stdin):  ``TIME <seconds>``, ``VIEWER waiting|connected``, ``END``
    banner -> parent (stdout): ``READY``, ``HEARTBEAT``, ``STOP button|hotkey``,
                               ``HOTKEY_UNAVAILABLE``

The parent fails closed: if the banner does not show READY, the session never starts;
if heartbeats stop, if the process exits, or if stdin closes, the session stops.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import queue
import sys
import threading
import time
from typing import Any, Callable

RED = "#b00020"
HOTKEY_TEXT = "Ctrl+Alt+Shift+Q"
BANNER_READY_TIMEOUT = 12.0
HEARTBEAT_MAX_AGE = 3.0

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
VK_Q = 0x51
WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001
HOTKEY_ID = 0xA5A5

CHILD_TOKENS = frozenset({"READY", "HEARTBEAT", "STOP button", "STOP hotkey", "HOTKEY_UNAVAILABLE"})


class RemoteError(RuntimeError):
    """Startup or runtime failure; the message is shown to the owner."""


# ---------------------------------------------------------------------------
# Pure helpers (tested offline)
# ---------------------------------------------------------------------------

def format_remaining(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def banner_lines(remaining: float, viewer: str) -> tuple[str, str]:
    state = "connected" if viewer == "connected" else "waiting for viewer"
    first = f"REMOTE CONTROL ACTIVE · time left {format_remaining(remaining)} · viewer {state}"
    second = f"Stop now: press {HOTKEY_TEXT} or the Stop button"
    return first, second


def parse_child_line(line: str) -> str | None:
    value = line.strip()
    return value if value in CHILD_TOKENS else None


def parse_parent_command(line: str) -> tuple[str, str] | None:
    parts = line.strip().split(" ", 1)
    if parts[0] == "TIME" and len(parts) == 2 and parts[1].isdigit():
        return "TIME", parts[1]
    if parts[0] == "VIEWER" and len(parts) == 2 and parts[1] in {"waiting", "connected"}:
        return "VIEWER", parts[1]
    if parts == ["END"]:
        return "END", ""
    return None


# ---------------------------------------------------------------------------
# Parent side
# ---------------------------------------------------------------------------

class RemoteBanner:
    """Owns the banner process. ``on_stop(reason)`` is called at most once, from the loop."""

    def __init__(self, process: Any, on_stop: Callable[[str], None]) -> None:
        self.process = process
        self.on_stop = on_stop
        self.last_beat = 0.0
        self.hotkey_ok = True
        self._reader: asyncio.Task | None = None
        self._closing = False
        self._reported = False

    @classmethod
    async def open(cls, on_stop: Callable[[str], None], *, argv: list[str] | None = None,
                   ready_timeout: float = BANNER_READY_TIMEOUT) -> "RemoteBanner":
        from ..sharing import _spawn  # shared helper: no orphaned children on cancellation
        command = argv or [sys.executable, "-u", "-m", "assistant.remote.banner"]
        # Run the child from the project folder so "-m assistant.remote.banner" always imports,
        # however the bot itself was started.
        project = str(Path(__file__).resolve().parents[2])
        process = await _spawn(*command, stdin=asyncio.subprocess.PIPE, cwd=project,
                               stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        banner = cls(process, on_stop)
        try:
            line = await asyncio.wait_for(process.stdout.readline(), ready_timeout)
            if line.strip() != b"READY" or process.returncode is not None:
                raise RemoteError("The red remote-control banner could not be shown, so nothing was started.")
            banner.last_beat = time.monotonic()
            banner._reader = asyncio.create_task(banner._read())
            return banner
        except BaseException:
            await banner.close()
            raise

    async def _read(self) -> None:
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    break
                token = parse_child_line(line.decode("utf-8", "replace"))
                if token == "HEARTBEAT":
                    self.last_beat = time.monotonic()
                elif token in ("STOP button", "STOP hotkey"):
                    self._report("button" if token.endswith("button") else "hotkey")
                    return
                elif token == "HOTKEY_UNAVAILABLE":
                    self.hotkey_ok = False
        except (OSError, asyncio.CancelledError):
            return
        if not self._closing:
            self._report("banner closed")

    def _report(self, reason: str) -> None:
        if self._closing or self._reported:
            return
        self._reported = True
        self.last_beat = 0.0
        self.on_stop(reason)

    def send(self, line: str) -> None:
        stdin = self.process.stdin
        if self._closing or stdin is None or self.process.returncode is not None:
            return
        try:
            stdin.write(line.encode("ascii", "replace") + b"\n")
        except (OSError, RuntimeError):
            self.last_beat = 0.0

    def _write_end(self) -> None:
        stdin = self.process.stdin
        if stdin is None or self.process.returncode is not None:
            return
        try:
            stdin.write(b"END\n")
        except (OSError, RuntimeError):
            pass

    def visible(self, max_age: float = HEARTBEAT_MAX_AGE) -> bool:
        return (self.process.returncode is None and self.last_beat > 0 and
                time.monotonic() - self.last_beat < max_age)

    async def close(self) -> None:
        if self._closing:
            return
        self._write_end()          # explicit END first; the child also exits on EOF
        self._closing = True
        if self._reader is not None:
            self._reader.cancel()
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
        except (OSError, RuntimeError):
            pass
        try:
            await asyncio.wait_for(self.process.wait(), 2)
        except asyncio.TimeoutError:
            try:
                self.process.kill()
            except ProcessLookupError:
                pass
            await self.process.wait()


# ---------------------------------------------------------------------------
# Child side (runs in its own process)
# ---------------------------------------------------------------------------

def _register_hotkey() -> bool:
    if sys.platform != "win32":
        return False
    import ctypes
    return bool(ctypes.windll.user32.RegisterHotKey(
        None, HOTKEY_ID, MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT, VK_Q))


def _poll_hotkey() -> bool:
    import ctypes
    from ctypes import wintypes

    class MSG(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT), ("wParam", wintypes.WPARAM),
                    ("lParam", wintypes.LPARAM), ("time", wintypes.DWORD), ("pt", wintypes.POINT)]

    message = MSG()
    got = ctypes.windll.user32.PeekMessageW(ctypes.byref(message), None, WM_HOTKEY, WM_HOTKEY, PM_REMOVE)
    return bool(got) and message.wParam == HOTKEY_ID


def _unregister_hotkey() -> None:
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.UnregisterHotKey(None, HOTKEY_ID)


def main() -> int:
    try:
        import tkinter as tk
    except Exception:
        return 1
    try:
        root = tk.Tk()
    except Exception:
        return 1
    commands: queue.Queue = queue.Queue()

    def read_stdin() -> None:
        for raw in sys.stdin:
            commands.put(raw)
        commands.put("END\n")  # parent closed the pipe: stop with it

    threading.Thread(target=read_stdin, daemon=True).start()
    root.title("REMOTE CONTROL ACTIVE")
    root.configure(background=RED)
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    width, height = 720, 70
    x = max(0, (root.winfo_screenwidth() - width) // 2)
    root.geometry(f"{width}x{height}+{x}+0")
    state = {"remaining": 0.0, "viewer": "waiting", "done": False}
    first = tk.Label(root, text="", bg=RED, fg="white", font=("Segoe UI", 11, "bold"))
    first.pack(pady=(6, 0))
    second = tk.Frame(root, bg=RED)
    second.pack(pady=(2, 4))
    hint = tk.Label(second, text="", bg=RED, fg="white", font=("Segoe UI", 9))
    hint.pack(side="left", padx=(0, 10))

    def finish(reason: str | None) -> None:
        if state["done"]:
            return
        state["done"] = True
        if reason:
            print(f"STOP {reason}", flush=True)
        root.destroy()

    tk.Button(second, text="Stop", command=lambda: finish("button"), bg="white", fg="black",
              width=8).pack(side="left")
    hotkey_ok = _register_hotkey()
    if not hotkey_ok:
        print("HOTKEY_UNAVAILABLE", flush=True)
    root.update_idletasks()
    root.update()
    if not root.winfo_viewable():
        _unregister_hotkey()
        root.destroy()
        return 1
    print("READY", flush=True)

    def tick() -> None:
        if state["done"]:
            return
        try:
            while True:
                try:
                    raw = commands.get_nowait()
                except queue.Empty:
                    break
                parsed = parse_parent_command(raw)
                if parsed is None:
                    continue
                name, value = parsed
                if name == "TIME":
                    state["remaining"] = float(value)
                elif name == "VIEWER":
                    state["viewer"] = value
                elif name == "END":
                    finish(None)
                    return
            if hotkey_ok and _poll_hotkey():
                finish("hotkey")
                return
            text_one, text_two = banner_lines(state["remaining"], state["viewer"])
            first.config(text=text_one)
            hint.config(text=text_two)
            root.attributes("-topmost", True)
            print("HEARTBEAT", flush=True)
            root.after(500, tick)
        except tk.TclError:
            finish(None)

    root.after(100, tick)
    root.mainloop()
    _unregister_hotkey()
    return 0


if __name__ == "__main__":
    sys.exit(main())
