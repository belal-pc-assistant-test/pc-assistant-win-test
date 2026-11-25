"""Visible, consent-gated and bounded screen capture. No frame is saved to disk."""
from __future__ import annotations
import asyncio
import base64
import io
import os
import sys
import time
import uuid
from datetime import timedelta
from typing import Any, Callable

from telegram import InputMediaPhoto
from telegram.error import RetryAfter
from .actions.base import abort_requested
from . import audit

MIN_INTERVAL = 2
MAX_INTERVAL = 60
DEFAULT_INTERVAL = 3
DEFAULT_MINUTES = 10
MAX_MINUTES = 60
FALLBACK_INTERVAL = 30
INDICATOR_TIMEOUT = 8
HEARTBEAT_TIMEOUT = 2.5


class SharingError(RuntimeError):
    pass


async def _spawn(*argv, **kwargs):
    """Do not orphan a just-spawned capture or indicator if cancellation races spawn."""
    spawning = asyncio.create_task(asyncio.create_subprocess_exec(*argv, **kwargs))
    try:
        return await asyncio.shield(spawning)
    except asyncio.CancelledError:
        try:
            process = await spawning
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
        except Exception:
            pass
        raise


class ScreenIndicator:
    """Launch a visible topmost Tk window and monitor its visibility heartbeat."""
    def __init__(self, process):
        self.process = process
        self.last_visible = time.monotonic()
        self.reader = None

    @classmethod
    async def open(cls):
        process = await _spawn(
            sys.executable, '-u', '-m', 'assistant.screen_indicator',
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        instance = cls(process)
        try:
            line = await asyncio.wait_for(process.stdout.readline(), INDICATOR_TIMEOUT)
            if line.strip() != b'READY' or process.returncode is not None:
                raise SharingError('A visible screen-sharing indicator could not be shown; no capture started.')
            instance.reader = asyncio.create_task(instance._read_heartbeats())
            return instance
        except BaseException:
            await instance.close()
            raise

    async def _read_heartbeats(self):
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line or line.strip() == b'HIDDEN':
                    self.last_visible = 0
                    return
                if line.strip() == b'VISIBLE':
                    self.last_visible = time.monotonic()
        except (OSError, asyncio.CancelledError):
            self.last_visible = 0

    def visible(self) -> bool:
        return (self.process.returncode is None and self.last_visible > 0 and
                time.monotonic() - self.last_visible < HEARTBEAT_TIMEOUT)

    def terminate_now(self):
        self.last_visible = 0
        if self.process.returncode is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass

    async def close(self):
        self.terminate_now()
        if self.reader:
            self.reader.cancel()
        try:
            await asyncio.wait_for(self.process.wait(), 2)
        except asyncio.TimeoutError:
            try:
                self.process.kill()
            except ProcessLookupError:
                pass
            await self.process.wait()


def foreground_window_handle() -> int:
    """Snapshot the foreground window before the indicator can affect focus."""
    if sys.platform != 'win32':
        return 0
    try:
        import ctypes
        getter = ctypes.windll.user32.GetForegroundWindow
        getter.restype = ctypes.c_void_p
        return int(getter() or 0)
    except Exception:
        return 0


async def capture_frame(owner, width: int, quality: int, title=None, handle=0) -> bytes:
    """Capture in a short-lived killable child; bytes are returned through a pipe."""
    if owner.stopping or not owner.indicator or not owner.indicator.visible():
        raise SharingError('Screen indicator is not visible.')
    child_environment = None  # only the Windows PowerShell capture needs the clean environment
    if title is None:
        argv = [sys.executable, '-m', 'assistant.capture_worker', str(width), str(quality)]
    else:
        from .actions.base import powershell_exe
        from .actions.system import WINDOW_SHOT_PS
        from .winshell import child_env
        title64 = base64.b64encode(title.encode('utf-8')).decode('ascii')
        script = WINDOW_SHOT_PS.replace('{title}', title64).replace('{handle}', str(int(handle)))
        argv = powershell_exe() + [script]
        child_environment = child_env() if os.name == 'nt' else None
    process = await _spawn(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=child_environment,
    )
    owner.capture_process = process
    try:
        if owner.stopping or not owner.indicator.visible():
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
            raise SharingError('Sharing stopped before capture.')
        data, _ = await process.communicate()
        if process.returncode != 0 or not data or len(data) > 30_000_000:
            raise SharingError('Screen capture failed.')
        if title is not None:
            from PIL import Image
            from .actions.core import encode_photo
            png = base64.b64decode(b''.join(data.split()), validate=True)
            with Image.open(io.BytesIO(png)) as image:
                data = encode_photo(image, width, quality)
        return data
    except asyncio.CancelledError:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
        raise
    finally:
        owner.capture_process = None


class CaptureLease:
    def __init__(self):
        self.indicator = None
        self.capture_process = None
        self.stopping = False
        self.task = None

    async def monitor(self):
        while not self.stopping:
            await asyncio.sleep(0.25)
            if not self.indicator or not self.indicator.visible() or abort_requested():
                self.stop_now()
                return

    def stop_now(self):
        self.stopping = True
        if self.capture_process and self.capture_process.returncode is None:
            try:
                self.capture_process.kill()
            except ProcessLookupError:
                pass
        if self.indicator:
            self.indicator.terminate_now()
        if self.task and not self.task.done():
            self.task.cancel()


def photo_stream(data: bytes) -> io.BytesIO:
    stream = io.BytesIO(data)
    stream.name = 'screen.jpg'
    return stream


def _seconds(retry: Any) -> float:
    value = retry.retry_after
    return max(2.0, value.total_seconds() if isinstance(value, timedelta) else float(value))


class WatchSession:
    """One asynchronous session; captures and uploads are strictly sequential."""
    def __init__(self, controller, user_id: int, chat_id: int, interval: int, minutes: int):
        self.controller = controller
        self.user_id = user_id
        self.chat_id = chat_id
        self.interval = interval
        self.minutes = minutes
        self.id = uuid.uuid4().hex[:12]
        self.indicator = None
        self.capture_process = None
        self.monitor = None
        self.task = None
        self.started = asyncio.get_running_loop().create_future()
        self.stopping = False
        self.reason = 'stopped'
        self.message_id = None
        self.last_fallback = -1e9
        self.announced = False

    def stop_now(self, reason: str):
        self.stopping = True
        self.reason = reason
        if self.capture_process and self.capture_process.returncode is None:
            try:
                self.capture_process.kill()
            except ProcessLookupError:
                pass
        if self.indicator:
            self.indicator.terminate_now()
        if self.task and not self.task.done():
            self.task.cancel()

    def safe(self) -> bool:
        return (not self.stopping and not abort_requested() and
                self.controller.config.is_allowed(self.user_id) and self.chat_id == self.user_id and
                self.indicator is not None and self.indicator.visible())

    async def _monitor_indicator(self):
        while not self.stopping:
            await asyncio.sleep(0.25)
            if not self.safe():
                self.stop_now('indicator closed or unavailable')
                return

    async def _photo(self, data: bytes):
        stream = photo_stream(data)
        try:
            sent = await self.controller.bot.send_photo(
                chat_id=self.chat_id, photo=stream,
                caption='🔴 SCREEN SHARING ACTIVE — /watch stop to end')
            self.message_id = sent.message_id
        finally:
            stream.close()

    async def _edit(self, data: bytes):
        stream = photo_stream(data)
        try:
            await self.controller.bot.edit_message_media(
                chat_id=self.chat_id, message_id=self.message_id,
                media=InputMediaPhoto(media=stream, caption='🔴 SCREEN SHARING ACTIVE — /watch stop to end'))
        finally:
            stream.close()

    async def _run(self):
        try:
            self.indicator = await self.controller.indicator_factory()
            if not self.safe():
                raise SharingError('The screen-sharing indicator is not visible.')
            await self.controller.bot.send_message(
                chat_id=self.chat_id,
                text=f'🔴 Screen sharing started. Full desktop, every {self.interval}s; stops after {self.minutes} minutes. /watch stop ends it now.')
            self.announced = True
            if not self.safe():
                raise SharingError('Indicator closed during startup.')
            self.monitor = asyncio.create_task(self._monitor_indicator())
            if not self.started.done():
                self.started.set_result((True, 'Screen sharing started; the desktop indicator is visible.'))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.minutes * 60
            next_frame = loop.time()
            while self.safe() and loop.time() < deadline:
                remaining = deadline - loop.time()
                if next_frame > loop.time():
                    await asyncio.sleep(min(next_frame - loop.time(), remaining, 1.0))
                    if loop.time() < next_frame:
                        continue
                if not self.safe() or loop.time() >= deadline:
                    break
                try:
                    data = await asyncio.wait_for(self.controller.capture(self, 1280, 70), deadline - loop.time())
                except asyncio.TimeoutError:
                    self.reason = 'time limit reached'
                    break
                if not self.safe() or loop.time() >= deadline:
                    break  # discard a frame if sharing stopped while capture was in flight
                delay = 0.0
                try:
                    if self.message_id is None:
                        await asyncio.wait_for(self._photo(data), deadline - loop.time())
                    else:
                        try:
                            await asyncio.wait_for(self._edit(data), deadline - loop.time())
                        except RetryAfter:
                            raise
                        except Exception as exc:
                            # Telegram may reject an unchanged frame. Never flood chat.
                            if 'message is not modified' not in str(exc).lower() and loop.time() - self.last_fallback >= FALLBACK_INTERVAL:
                                if not self.safe():
                                    break
                                self.last_fallback = loop.time()
                                await asyncio.wait_for(self._photo(data), deadline - loop.time())
                except RetryAfter as exc:
                    delay = _seconds(exc)
                except asyncio.TimeoutError:
                    self.reason = 'time limit reached'
                    break
                except Exception:
                    audit.warning('Screen frame upload failed')
                    delay = 5.0
                del data
                # Captures never overlap uploads. Missed frame slots are skipped.
                now = loop.time()
                next_frame += self.interval
                if next_frame <= now:
                    next_frame = now + self.interval
                next_frame = max(next_frame, now + delay)
            if not self.stopping:
                self.reason = 'time limit reached' if loop.time() >= deadline else 'indicator closed or unavailable'
        except asyncio.CancelledError:
            pass
        except Exception:
            self.reason = 'indicator or capture unavailable'
            if not self.started.done():
                self.started.set_result((False, 'Could not show the required indicator or start sharing; no capture started.'))
        finally:
            self.stopping = True
            if self.monitor:
                self.monitor.cancel()
            if self.indicator:
                await self.indicator.close()
            if not self.started.done():
                self.started.set_result((False, 'Screen sharing was stopped before starting.'))
            if self.controller.active is self:
                self.controller.active = None
            if self.announced:
                try:
                    await asyncio.wait_for(self.controller.bot.send_message(
                        chat_id=self.chat_id, text=f'⏹ Screen sharing stopped ({self.reason}).'), 5)
                except Exception:
                    audit.warning('Could not send sharing stop notice')


class WatchController:
    def __init__(self, bot, config, indicator_factory: Callable = ScreenIndicator.open,
                 capture: Callable = capture_frame):
        self.bot = bot
        self.config = config
        self.indicator_factory = indicator_factory
        self.capture = capture
        self.active: WatchSession | None = None

    async def start(self, user_id: int, chat_id: int, interval: int, minutes: int):
        if not self.config.is_allowed(user_id) or chat_id != user_id:
            return False, 'Screen sharing is restricted to an allowed private chat.'
        if not MIN_INTERVAL <= interval <= MAX_INTERVAL or not 1 <= minutes <= MAX_MINUTES:
            return False, 'Use 2–60 seconds and 1–60 minutes.'
        if self.active is not None:
            return False, 'A screen-sharing session is already active or starting.'
        session = WatchSession(self, user_id, chat_id, interval, minutes)
        self.active = session
        session.task = asyncio.create_task(session._run())
        def finished(task):
            # A task cancelled before its first instruction cannot run its finally block.
            if not session.started.done():
                session.started.set_result((False, 'Screen sharing was stopped before starting.'))
            if self.active is session:
                self.active = None
        session.task.add_done_callback(finished)
        try:
            return await asyncio.wait_for(asyncio.shield(session.started), INDICATOR_TIMEOUT + 6)
        except asyncio.TimeoutError:
            session.stop_now('startup timed out')
            return False, 'Indicator startup timed out; no screen capture started.'

    def stop_now(self, reason: str = 'requested') -> bool:
        if self.active is None:
            return False
        self.active.stop_now(reason)
        return True

    def speed(self, user_id: int, session_id: str, interval: int):
        session = self.active
        if session is None or session.stopping or session.user_id != user_id or session.id != session_id:
            return False, 'That screen-sharing session has ended or changed.'
        if not MIN_INTERVAL <= interval <= MAX_INTERVAL:
            return False, 'Watch speed must be 2–60 seconds.'
        session.interval = interval
        return True, f'Screen sharing interval changed to {interval}s.'
