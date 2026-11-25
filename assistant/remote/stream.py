"""Frame pipeline for remote view: capture, JPEG encoding, latest-frame broker, adaptation.

* Frames are kept in memory only. Nothing is written to disk.
* The broker holds only the newest frame. A viewer that is still sending the
  previous frame simply skips the frames that arrived meanwhile, so a slow
  connection shows fewer frames instead of falling further behind.
* ``QualityController`` lowers JPEG quality first, then the target frame rate,
  when sends are slow, and raises them again when sends are quick.
"""

from __future__ import annotations

import asyncio
import io
import sys
from dataclasses import dataclass
from typing import Callable

IS_WINDOWS = sys.platform == "win32"

MIN_FPS, DEFAULT_FPS, MAX_FPS = 5, 8, 10
MIN_QUALITY, START_QUALITY, MAX_QUALITY = 30, 60, 80
MAX_WIDTH = 1280
QUALITY_STEP = 6             # how far quality drops per congested send
QUALITY_RECOVER = 3          # how far quality rises after a run of quick sends
FAST_STREAK = 4              # consecutive quick sends before recovering
CONGESTED_SHARE = 0.8        # a send that uses >80% of a frame slot is congested
QUICK_SHARE = 0.35           # a send that uses <35% of a frame slot is quick


@dataclass(frozen=True)
class JpegFrame:
    jpeg: bytes
    width: int
    height: int


class QualityController:
    """Adapt JPEG quality and target fps from how long each frame took to send."""

    def __init__(self, quality: int = START_QUALITY, fps: int = DEFAULT_FPS) -> None:
        self.quality = quality
        self.fps = fps
        self._quick = 0

    @property
    def interval(self) -> float:
        return 1.0 / self.fps

    def observe(self, send_seconds: float) -> tuple[int, int]:
        """Feed one measured send time. Returns the new (quality, fps)."""
        budget = self.interval
        if send_seconds > budget * CONGESTED_SHARE:
            self._quick = 0
            if self.quality > MIN_QUALITY:
                self.quality = max(MIN_QUALITY, self.quality - QUALITY_STEP)
            elif self.fps > MIN_FPS:
                self.fps -= 1
        elif send_seconds < budget * QUICK_SHARE:
            self._quick += 1
            if self._quick >= FAST_STREAK:
                self._quick = 0
                self._recover()
        else:
            self._quick = 0
        return self.quality, self.fps

    def _recover(self) -> None:
        if self.fps < DEFAULT_FPS:
            self.fps += 1
        elif self.quality < MAX_QUALITY:
            self.quality = min(MAX_QUALITY, self.quality + QUALITY_RECOVER)
        elif self.fps < MAX_FPS:
            self.fps += 1


class FrameBroker:
    """Single-slot broker: publish overwrites, waiters get the newest frame only."""

    def __init__(self) -> None:
        self._seq = 0
        self._frame: JpegFrame | None = None
        self._waiters: list[asyncio.Future] = []

    @property
    def latest_seq(self) -> int:
        return self._seq

    def publish(self, frame: JpegFrame) -> None:
        self._seq += 1
        self._frame = frame
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    async def wait_newer(self, after: int) -> tuple[int, JpegFrame, int]:
        """Return (seq, frame, skipped) for the newest frame newer than ``after``."""
        while self._frame is None or self._seq <= after:
            waiter = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
            await waiter
        skipped = self._seq - after - 1 if after > 0 else 0
        return self._seq, self._frame, skipped


def capture_jpeg(quality: int, max_width: int = MAX_WIDTH) -> JpegFrame:
    """Grab the whole virtual desktop, shrink it and encode JPEG in memory."""
    from PIL import ImageGrab  # Pillow is already a dependency for screenshots

    image = ImageGrab.grab(**({"all_screens": True} if IS_WINDOWS else {}))
    try:
        image = image.convert("RGB")
        image.thumbnail((max_width, 4096))  # only ever shrinks, keeps aspect ratio
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=int(quality))
        return JpegFrame(buffer.getvalue(), image.width, image.height)
    finally:
        image.close()


CaptureFunc = Callable[[int], JpegFrame]
