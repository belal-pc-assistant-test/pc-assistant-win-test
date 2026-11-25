"""One approved remote-desktop session: start sequence, watchdog, stop and teardown.

Start order (each step is a precondition for the next, and nothing is exposed
before the red banner is confirmed visible):

    1. create the one-time access gate (fresh secret, expiry = now + minutes)
    2. open the red banner process and wait for READY        (fail closed)
    3. start the loopback server on 127.0.0.1 (random port)
    4. open a public tunnel (Cloudflare quick tunnel, then localhost.run)
    5. send the private link to the approved chat           (fail closed)
    6. capture the screen only while a viewer is connected

Stop is idempotent. It revokes the secret and cookie, closes viewer sockets, releases
held keys and mouse buttons, kills the tunnel process and closes the banner.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from .. import audit
from .banner import RemoteError, banner_lines
from .gate import TOTAL_FAILURE_CAP, AccessGate, EventLimiter, generate_secret
from .inputs import InputController, InputRejected, InputUnavailable, parse_event
from .server import RemoteServer
from .stream import CaptureFunc, FrameBroker, JpegFrame, QualityController
from .tunnel import PROVIDERS, TunnelError, TunnelProcess

DEFAULT_MINUTES = 15
MAX_MINUTES = 60
TUNNEL_TIMEOUT = 45.0
START_LIMIT = 90.0
CAPTURE_FAILURE_LIMIT = 5
WATCHDOG_INTERVAL = 1.0
INPUT_RATE = 120.0          # events per second accepted from the viewer
INPUT_BURST = 240.0

REASON_TEXT = {
    "requested": "stopped from Telegram",
    "panic": "stopped by /panic",
    "timeout": "time limit reached",
    "button": "Stop button on the PC",
    "hotkey": "hotkey Ctrl+Alt+Shift+Q on the PC",
    "banner lost": "the red banner stopped responding",
    "banner closed": "the red banner closed",
    "tunnel closed": "the public tunnel closed",
    "too many failed attempts": "too many failed access attempts",
    "capture failed": "screen capture failed repeatedly",
    "shutdown": "the assistant is shutting down",
    "could not deliver link": "the link could not be sent to Telegram",
    "startup failed": "startup failed; nothing stayed open",
    "startup timed out": "startup timed out",
}


@dataclass
class RemoteDeps:
    """Everything that touches the operating system, injectable for tests."""
    open_banner: Callable[[Callable[[str], None]], Awaitable[Any]]
    tunnel_argv: Callable[[str, int], list[str] | None]   # may download; run in a thread
    capture: CaptureFunc
    input_backend: Callable[[], Any]
    say: Callable[[str], Awaitable[None]]
    send_link: Callable[[str], Awaitable[None]]
    providers: tuple[str, ...] = PROVIDERS
    now: Callable[[], float] = time.monotonic


def link_message(link: str, minutes: int, expires_at: str) -> str:
    return (
        f"🖥️ Remote session started: FULL VIEW and CONTROL of this PC for {minutes} minute(s), "
        f"until {expires_at}. Your mouse and keyboard drive the PC.\n\n"
        "Private link (single use, do not forward it; it opens once and then locks):\n"
        f"{link}\n\n"
        "A red banner stays on the PC. Stop with the Stop button, Ctrl+Alt+Shift+Q, or /remote stop."
    )


class RemoteSession:
    def __init__(self, deps: RemoteDeps, *, user_id: int, minutes: int,
                 duration_seconds: float | None = None) -> None:
        self.deps = deps
        self.user_id = user_id
        self.minutes = minutes
        self.duration = float(duration_seconds if duration_seconds is not None else minutes * 60)
        self.state = "new"
        self.reason = ""
        self.provider = ""
        self.gate: AccessGate | None = None
        self.server: RemoteServer | None = None
        self.banner: Any = None
        self.tunnel: TunnelProcess | None = None
        self.input: InputController | None = None
        self.broker = FrameBroker()
        self.quality = QualityController()
        self.limiter = EventLimiter(INPUT_RATE, INPUT_BURST)
        self.deadline = 0.0
        self.started_at = 0.0
        self.frames = 0
        self.inputs = 0
        self.inputs_rejected = 0
        self.viewers_seen = 0
        self._tasks: list[asyncio.Task] = []
        self._background: set[asyncio.Task] = set()
        self._start_task: asyncio.Task | None = None
        self._closed = asyncio.Event()
        self._viewer_present = asyncio.Event()

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------
    async def start(self) -> tuple[bool, str]:
        if self.state != "new":
            return False, "This remote session already started."
        self.state = "starting"
        self._start_task = asyncio.current_task()
        try:
            await self._start()
            return True, (f"Remote session active for {self.minutes} minute(s). "
                          "The private link was sent in a separate message.")
        except asyncio.CancelledError:
            # Cancelled by stop()/panic/shutdown/timeout: teardown is already running or done.
            await self.stop("startup failed")
            return False, "Remote session was stopped during startup; nothing was exposed."
        except RemoteError as exc:
            await self.stop("startup failed")
            return False, str(exc)
        except Exception:  # never leak details; the audit log keeps only metadata
            audit.error("remote session startup failed", exc_info=True)
            await self.stop("startup failed")
            return False, "The remote session could not start; nothing was exposed."
        finally:
            # Once startup has finished, a later stop must never cancel this task.
            self._start_task = None

    def _ensure_starting(self) -> None:
        if self.state != "starting":
            raise RemoteError("The remote session was stopped during startup; nothing was exposed.")

    async def _start(self) -> None:
        self.deadline = self.deps.now() + self.duration
        secret = generate_secret()
        self.gate = AccessGate(secret, deadline=self.deadline, now=self.deps.now)
        self.input = InputController(self.deps.input_backend())
        self.banner = await self.deps.open_banner(self._on_banner_stop)
        self._ensure_starting()
        self.server = RemoteServer(
            gate=self.gate, broker=self.broker, on_input=self._on_input,
            on_viewer=self._on_viewer, on_sent=self.quality.observe,
            stats=self._stats, on_denied=self._on_denied,
        )
        port = await self.server.start()
        self._ensure_starting()
        public_url = await self._open_tunnel(port)
        self._ensure_starting()
        self.server.allowed_hosts = {urlsplit(public_url).netloc}  # setter normalizes case and default port
        self.state = "active"
        self.started_at = self.deps.now()
        expires = time.strftime("%H:%M", time.localtime(time.time() + self.duration))
        link = f"{public_url}/connect/{secret}"
        del secret
        audit.event("remote_started", user_id=self.user_id, minutes=self.minutes, provider=self.provider)
        self.banner.send("VIEWER waiting")
        self._tasks = [asyncio.create_task(self._capture_loop()),
                       asyncio.create_task(self._watchdog())]
        try:
            await self.deps.send_link(link_message(link, self.minutes, expires))
        except Exception as exc:
            await self.stop("could not deliver link")
            raise RemoteError("The link could not be sent to Telegram, so the session was stopped.") from exc
        finally:
            link = ""

    async def _open_tunnel(self, port: int) -> str:
        for provider in self.deps.providers:
            try:
                argv = await asyncio.to_thread(self.deps.tunnel_argv, provider, port)
                if not argv:
                    continue
                process = TunnelProcess.spawn(provider, argv)
            except (TunnelError, OSError):
                audit.warning("tunnel unavailable", provider=provider)
                continue
            self.tunnel = process
            url = await process.wait_for_url(TUNNEL_TIMEOUT)
            if url and urlsplit(url).scheme == "https":
                self.provider = provider
                audit.event("tunnel_ready", provider=provider)
                return url
            audit.warning("tunnel gave no address", provider=provider)
            process.close()
            self.tunnel = None
        raise RemoteError("No public tunnel could be opened (free tunnels are sometimes unavailable). "
                          "Nothing was exposed.")

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------
    async def _capture_loop(self) -> None:
        failures = 0
        while self.state == "active":
            await self._viewer_present.wait()   # capture only while someone is watching
            started = self.deps.now()
            try:
                frame: JpegFrame = await asyncio.to_thread(self.deps.capture, self.quality.quality)
            except Exception:
                failures += 1
                if failures >= CAPTURE_FAILURE_LIMIT:
                    await self.stop("capture failed")
                    return
                await asyncio.sleep(1.0)
                continue
            failures = 0
            self.broker.publish(frame)
            self.frames += 1
            elapsed = self.deps.now() - started
            await asyncio.sleep(max(0.0, self.quality.interval - elapsed))

    async def _watchdog(self) -> None:
        while self.state == "active":
            await asyncio.sleep(WATCHDOG_INTERVAL)
            remaining = self.deadline - self.deps.now()
            if remaining <= 0:
                await self.stop("timeout")
                return
            if self.banner is not None:
                self.banner.send(f"TIME {int(remaining)}")
                if not self.banner.visible():
                    await self.stop("banner lost")
                    return
            if self.tunnel is None or not self.tunnel.alive():
                await self.stop("tunnel closed")
                return
            if self.gate is not None and self.gate.total_failures >= TOTAL_FAILURE_CAP:
                await self.stop("too many failed attempts")
                return

    def _spawn(self, coro) -> None:
        """Fire-and-forget work that keeps a reference so it cannot be collected early."""
        task = asyncio.get_running_loop().create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _on_banner_stop(self, reason: str) -> None:
        if self.state in ("active", "starting"):
            self._spawn(self.stop(reason))

    def _on_viewer(self, connected: bool) -> None:
        if self.state != "active":
            return
        if connected:
            self.viewers_seen += 1
            self._viewer_present.set()
            if self.banner is not None:
                self.banner.send("VIEWER connected")
            audit.event("viewer_connected", user_id=self.user_id, viewers=self.viewers_seen)
            self._spawn(self._notify(
                "👁️ A viewer connected to the remote session. If this was not you, send /remote stop now."))
        else:
            self._viewer_present.clear()
            if self.banner is not None:
                self.banner.send("VIEWER waiting")
            audit.event("viewer_disconnected", user_id=self.user_id)

    def _on_denied(self, decision: str) -> None:
        audit.event("remote_access_denied", user_id=self.user_id)

    def _on_input(self, raw: Any) -> None:
        if self.state != "active" or self.input is None:
            return
        if not self.limiter.allow():
            self.inputs_rejected += 1
            return
        try:
            event = parse_event(raw)
        except InputRejected:
            self.inputs_rejected += 1
            return
        try:
            self.input.apply(event)
            self.inputs += 1
        except InputUnavailable:
            self.inputs_rejected += 1

    def _stats(self) -> dict:
        remaining = max(0, int(self.deadline - self.deps.now()))
        return {"remaining": remaining, "quality": self.quality.quality,
                "fps_target": self.quality.fps, "inputs": self.inputs, "viewers": self.viewers_seen}

    async def _notify(self, text: str) -> None:
        try:
            await self.deps.say(text)
        except Exception:
            audit.warning("could not send a remote session notice")

    # ------------------------------------------------------------------
    # Stopping
    # ------------------------------------------------------------------
    async def stop(self, reason: str = "requested") -> None:
        if self.state in ("stopping", "stopped"):
            # A second caller (e.g. the watchdog racing /remote stop) still releases anything
            # that a half-finished startup left open. Each resource is detached before its close.
            await self._teardown()
            return
        previous = self.state
        self.state = "stopping"
        self.reason = reason
        current = asyncio.current_task()
        if self.gate is not None:
            self.gate.revoke()
        self._viewer_present.clear()
        for task in [*self._tasks, self._start_task]:
            if task is not None and task is not current and not task.done():
                task.cancel()
        await self._teardown()
        duration = int(self.deps.now() - self.started_at) if self.started_at else 0
        audit.event("remote_stopped", user_id=self.user_id, reason=reason, duration=duration,
                    frames=self.frames, inputs=self.inputs, viewers=self.viewers_seen,
                    rejected=self.gate.total_failures if self.gate else 0)
        if previous == "active":
            await self._notify(f"⏹ Remote session stopped — {REASON_TEXT.get(reason, reason)}. "
                               "The link no longer works.")
        self.state = "stopped"
        self._closed.set()

    async def _teardown(self) -> None:
        if self.input is not None:
            try:
                self.input.release_all()
            except Exception:
                audit.warning("could not release held keys cleanly")
        server, self.server = self.server, None
        if server is not None:
            await server.close()
        tunnel, self.tunnel = self.tunnel, None
        if tunnel is not None:
            tunnel.close()
        banner, self.banner = self.banner, None
        if banner is not None:
            await banner.close()

    async def wait_closed(self) -> None:
        await self._closed.wait()


class RemoteController:
    """Bot-facing singleton: one session at a time, owner and private chat only."""

    def __init__(self, *, is_allowed: Callable[[int | None], bool], max_minutes: int,
                 default_minutes: int, deps_factory: Callable[[int], RemoteDeps],
                 platform_ok: Callable[[], bool]) -> None:
        self.is_allowed = is_allowed
        self.max_minutes = min(int(max_minutes), MAX_MINUTES)
        self.default_minutes = int(default_minutes)
        self.deps_factory = deps_factory
        self.platform_ok = platform_ok
        self.active: RemoteSession | None = None
        self._watcher: asyncio.Task | None = None

    def running(self) -> bool:
        return self.active is not None and self.active.state in ("starting", "active", "stopping")

    def check_request(self, user_id: int, chat_id: int, minutes: int) -> str | None:
        """Return a refusal message, or None when a plan may be proposed."""
        if chat_id != user_id or not self.is_allowed(user_id):
            return "Remote sessions are restricted to the allowed private chat."
        if not self.platform_ok():
            return "Remote control runs only on Windows."
        if not 1 <= minutes <= self.max_minutes:
            return f"Use 1–{self.max_minutes} minutes (default {self.default_minutes})."
        if self.running():
            return "A remote session is already running. Use /remote stop first."
        return None

    async def start(self, user_id: int, chat_id: int, minutes: int) -> tuple[bool, str]:
        refusal = self.check_request(user_id, chat_id, minutes)
        if refusal is not None:
            return False, refusal
        session = RemoteSession(self.deps_factory(chat_id), user_id=user_id, minutes=minutes)
        self.active = session
        task = asyncio.create_task(session.start())
        try:
            ok, message = await asyncio.wait_for(asyncio.shield(task), START_LIMIT)
        except asyncio.TimeoutError:
            await session.stop("startup timed out")
            ok, message = False, "Startup timed out; nothing was exposed."
        if ok:
            self._watcher = asyncio.create_task(self._forget_when_closed(session))
        else:
            if self.active is session:
                self.active = None
        return ok, message

    async def _forget_when_closed(self, session: RemoteSession) -> None:
        await session.wait_closed()
        if self.active is session:
            self.active = None

    async def stop(self, reason: str = "requested") -> bool:
        session = self.active
        if session is None or session.state in ("stopped",):
            return False
        await session.stop(reason)
        return True

    def status_text(self) -> str:
        session = self.active
        if session is None or session.state == "stopped":
            return "No remote session is running."
        remaining = max(0, int(session.deadline - session.deps.now()))
        connected = bool(session.server and session.server.viewer_connected)
        first, _second = banner_lines(remaining, "connected" if connected else "waiting")
        return (f"Remote session: {session.state}\n{first}\n"
                f"Viewer: {'connected' if connected else 'not connected'}\n"
                f"Provider: {session.provider or 'pending'}")
