"""Remote mouse and keyboard: strict event validation, key mapping and OS injection.

Only the event kinds below are accepted; everything else is rejected before it
reaches the operating system. Key values are never logged and are excluded from
``repr`` so they cannot leak through exceptions or debugging output.
Stuck keys and buttons are released when the session ends or the viewer loses focus.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from typing import Any

MOUSE_KINDS = frozenset({"move", "mdown", "mup", "wheel"})
KEY_KINDS = frozenset({"key"})
CONTROL_KINDS = frozenset({"releaseall"})
ALL_KINDS = MOUSE_KINDS | KEY_KINDS | CONTROL_KINDS
MAX_KEY_CHARS = 32
MAX_WHEEL_DELTA = 1000.0
BUTTONS = {0: "left", 1: "middle", 2: "right"}


class InputRejected(ValueError):
    """The event is not one we accept (content is deliberately not echoed)."""


class InputUnavailable(RuntimeError):
    """This system cannot inject input (remote control needs Windows)."""


@dataclass(frozen=True)
class InputEvent:
    kind: str
    x: float = 0.0
    y: float = 0.0
    button: int = 0
    delta: float = 0.0
    key: str = field(default="", repr=False)
    code: str = field(default="", repr=False)
    down: bool = False
    ctrl: bool = False
    alt: bool = False
    shift: bool = False
    meta: bool = False


def _number(value: Any, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise InputRejected("bad number")
    return min(high, max(low, float(value)))


def _flag(raw: dict, name: str) -> bool:
    value = raw.get(name, False)
    if not isinstance(value, bool):
        raise InputRejected("bad flag")
    return value


def parse_event(raw: Any) -> InputEvent:
    """Validate one decoded JSON object from the viewer. Coordinates are clamped to 0..1."""
    if not isinstance(raw, dict):
        raise InputRejected("not an object")
    kind = raw.get("k")
    if kind not in ALL_KINDS:
        raise InputRejected("unknown event kind")
    if kind in MOUSE_KINDS and kind != "wheel":
        x = _number(raw.get("x"), 0.0, 1.0)
        y = _number(raw.get("y"), 0.0, 1.0)
        if kind == "move":
            return InputEvent(kind, x=x, y=y)
        button = raw.get("b", 0)
        if isinstance(button, bool) or button not in BUTTONS:
            raise InputRejected("bad button")
        return InputEvent(kind, x=x, y=y, button=int(button))
    if kind == "wheel":
        delta = _number(raw.get("dy", 0), -MAX_WHEEL_DELTA, MAX_WHEEL_DELTA)
        return InputEvent(kind, delta=delta)
    if kind == "key":
        key = raw.get("key")
        code = raw.get("code", "")
        if not isinstance(key, str) or not 1 <= len(key) <= MAX_KEY_CHARS:
            raise InputRejected("bad key")
        if not isinstance(code, str) or len(code) > MAX_KEY_CHARS:
            raise InputRejected("bad code")
        return InputEvent(kind, key=key, code=code, down=_flag(raw, "down"),
                          ctrl=_flag(raw, "ctrl"), alt=_flag(raw, "alt"),
                          shift=_flag(raw, "shift"), meta=_flag(raw, "meta"))
    return InputEvent(kind)


# ---------------------------------------------------------------------------
# Key mapping (browser KeyboardEvent -> Windows virtual-key codes)
# ---------------------------------------------------------------------------

_NAMED_VK = {
    "Enter": 0x0D, "Backspace": 0x08, "Tab": 0x09, "Escape": 0x1B, " ": 0x20,
    "Delete": 0x2E, "Insert": 0x2D, "Home": 0x24, "End": 0x23,
    "PageUp": 0x21, "PageDown": 0x22, "ArrowLeft": 0x25, "ArrowUp": 0x26,
    "ArrowRight": 0x27, "ArrowDown": 0x28, "Shift": 0x10, "Control": 0x11,
    "Alt": 0x12, "Meta": 0x5B, "CapsLock": 0x14, "NumLock": 0x90,
    "ScrollLock": 0x91, "PrintScreen": 0x2C, "Pause": 0x13, "ContextMenu": 0x5D,
}
_CODE_VK = {
    "Minus": 0xBD, "Equal": 0xBB, "Comma": 0xBC, "Period": 0xBE, "Slash": 0xBF,
    "Semicolon": 0xBA, "Quote": 0xDE, "BracketLeft": 0xDB, "BracketRight": 0xDD,
    "Backslash": 0xDC, "Backquote": 0xC0,
}
EXTENDED_VK = frozenset({0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E,
                         0x5B, 0x5C, 0x5D, 0x90})


def vk_for(key: str, code: str) -> int | None:
    """Virtual-key code for a key press, or None when the key is not mapped."""
    if key in _NAMED_VK:
        return _NAMED_VK[key]
    if len(key) == 2 and key[0] == "F" and key[1] in "123456789":
        return 0x70 + int(key[1]) - 1
    if len(key) == 3 and key[0] == "F" and key[1:] in ("10", "11", "12"):
        return 0x70 + int(key[1:]) - 1
    if len(code) == 4 and code.startswith("Key") and code[3].isalpha():
        return ord(code[3].upper())
    if len(code) == 6 and code.startswith("Digit") and code[5].isdigit():
        return ord(code[5])
    if code in _CODE_VK:
        return _CODE_VK[code]
    if len(key) == 1 and key.isascii() and key.isalnum():
        return ord(key.upper())
    return None


def is_printable(event: InputEvent) -> bool:
    """Printable text without Ctrl/Alt/Meta is injected as Unicode text."""
    return (len(event.key) == 1 and event.key.isprintable() and event.key != " " and
            not (event.ctrl or event.alt or event.meta))


# ---------------------------------------------------------------------------
# Controller: applies validated events and remembers what is held down
# ---------------------------------------------------------------------------

class InputController:
    def __init__(self, backend: Any) -> None:
        self.backend = backend
        self._keys: set[tuple[str, Any]] = set()
        self._buttons: set[int] = set()

    def apply(self, event: InputEvent) -> None:
        if event.kind == "releaseall":
            self.release_all()
        elif event.kind == "move":
            self._move(event)
        elif event.kind in ("mdown", "mup"):
            self._move(event)
            down = event.kind == "mdown"
            self.backend.mouse(event.button, down)
            if down:
                self._buttons.add(event.button)
            else:
                self._buttons.discard(event.button)
        elif event.kind == "wheel":
            # Browsers report about 100 units per notch; Windows uses 120 per notch.
            self.backend.wheel(int(round(-event.delta * 1.2)))
        elif event.kind == "key":
            self._key(event)

    def _move(self, event: InputEvent) -> None:
        left, top, width, height = self.backend.screen_rect()
        px = left + round(event.x * max(0, width - 1))
        py = top + round(event.y * max(0, height - 1))
        self.backend.move(int(px), int(py))

    def _key(self, event: InputEvent) -> None:
        if is_printable(event):
            token = ("char", event.key)
            if event.down:
                self.backend.unicode(event.key, True)
                self._keys.add(token)
            elif token in self._keys:
                self.backend.unicode(event.key, False)
                self._keys.discard(token)
            return
        vk = vk_for(event.key, event.code)
        if vk is None:
            return  # unmapped keys are ignored silently
        token = ("vk", vk)
        if event.down:
            self.backend.key(vk, True, extended=vk in EXTENDED_VK)
            self._keys.add(token)
        elif token in self._keys:
            self.backend.key(vk, False, extended=vk in EXTENDED_VK)
            self._keys.discard(token)

    def release_all(self) -> None:
        """Release every key and mouse button this controller pressed."""
        for kind, value in list(self._keys):
            if kind == "char":
                self.backend.unicode(value, False)
            else:
                self.backend.key(value, False, extended=value in EXTENDED_VK)
        self._keys.clear()
        for button in list(self._buttons):
            self.backend.mouse(button, False)
        self._buttons.clear()


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class RecordingBackend:
    """Records calls instead of touching the system (tests and dry runs)."""

    def __init__(self, rect: tuple[int, int, int, int] = (0, 0, 1920, 1080)) -> None:
        self.rect = rect
        self.calls: list[tuple] = []

    def screen_rect(self):
        return self.rect

    def move(self, x, y):
        self.calls.append(("move", x, y))

    def mouse(self, button, down):
        self.calls.append(("mouse", button, down))

    def wheel(self, delta):
        self.calls.append(("wheel", delta))

    def key(self, vk, down, extended=False):
        self.calls.append(("key", vk, down, extended))

    def unicode(self, char, down):
        self.calls.append(("unicode", char, down))


class WindowsBackend:
    """Injects input with user32 SendInput / SetCursorPos. Windows only."""

    _MOUSE_FLAGS = {(0, True): 0x0002, (0, False): 0x0004, (2, True): 0x0008,
                    (2, False): 0x0010, (1, True): 0x0020, (1, False): 0x0040}
    _WHEEL = 0x0800
    _KEYUP = 0x0002
    _KEY_EXTENDED = 0x0001
    _UNICODE = 0x0004

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise InputUnavailable("Remote control is available only on Windows.")
        import ctypes
        from ctypes import wintypes

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.c_size_t)]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                        ("wParamH", wintypes.WORD)]

        class _Union(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", _Union)]

        self._ctypes = ctypes
        self._INPUT = INPUT
        self._MOUSEINPUT = MOUSEINPUT
        self._KEYBDINPUT = KEYBDINPUT
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
        self._user32.SendInput.restype = wintypes.UINT

    def _send(self, item) -> None:
        sent = self._user32.SendInput(1, self._ctypes.byref(item), self._ctypes.sizeof(self._INPUT))
        if sent != 1:
            raise InputUnavailable("Windows refused the input event (secure desktop or UAC window?).")

    def screen_rect(self):
        metrics = self._user32.GetSystemMetrics
        return (metrics(76), metrics(77), metrics(78), metrics(79))  # virtual screen

    def move(self, x, y):
        if not self._user32.SetCursorPos(int(x), int(y)):
            raise InputUnavailable("Could not move the mouse cursor.")

    def mouse(self, button, down):
        flags = self._MOUSE_FLAGS[(button, down)]
        item = self._INPUT(type=0)
        item.mi = self._MOUSEINPUT(dwFlags=flags)
        self._send(item)

    def wheel(self, delta):
        item = self._INPUT(type=0)
        item.mi = self._MOUSEINPUT(dwFlags=self._WHEEL, mouseData=int(delta) & 0xFFFFFFFF)
        self._send(item)

    def key(self, vk, down, extended=False):
        flags = (0 if down else self._KEYUP) | (self._KEY_EXTENDED if extended else 0)
        item = self._INPUT(type=1)
        item.ki = self._KEYBDINPUT(wVk=vk, dwFlags=flags)
        self._send(item)

    def unicode(self, char, down):
        base = self._UNICODE | (0 if down else self._KEYUP)
        data = char.encode("utf-16-le")
        for offset in range(0, len(data), 2):
            unit = int.from_bytes(data[offset:offset + 2], "little")
            item = self._INPUT(type=1)
            item.ki = self._KEYBDINPUT(wVk=0, wScan=unit, dwFlags=base)
            self._send(item)
