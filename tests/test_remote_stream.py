"""Frame pipeline: adaptive quality/fps, latest-frame broker (drops for slow viewers), capture encoding."""

from __future__ import annotations

import asyncio
import io
import unittest
from unittest import mock

from _bootstrap import PROJECT_DIR
assert PROJECT_DIR.is_dir()
from assistant.remote import stream
from assistant.remote.stream import (MAX_FPS, MAX_QUALITY, MIN_FPS, MIN_QUALITY, FrameBroker,
                                     JpegFrame, QualityController)


class QualityControllerTests(unittest.TestCase):
    def test_congestion_lowers_quality_first_then_fps_and_never_below_floors(self):
        controller = QualityController()
        seen = []
        for _ in range(60):
            # Every send takes a whole frame slot at the *current* target, i.e. congested.
            seen.append(controller.observe(controller.interval))
        self.assertEqual(controller.quality, MIN_QUALITY)
        self.assertEqual(controller.fps, MIN_FPS)
        # Quality reached its floor before fps moved.
        qualities = [q for q, _ in seen]
        fpss = [f for _, f in seen]
        first_fps_drop = next(i for i, f in enumerate(fpss) if f < fpss[0])
        self.assertEqual(qualities[first_fps_drop], MIN_QUALITY)

    def test_quick_sends_recover_fps_then_quality_up_to_ceilings(self):
        controller = QualityController(quality=MIN_QUALITY, fps=MIN_FPS)
        for _ in range(400):
            controller.observe(0.0)
        self.assertEqual(controller.fps, MAX_FPS)
        self.assertEqual(controller.quality, MAX_QUALITY)

    def test_moderate_sends_hold_steady(self):
        controller = QualityController()
        before = (controller.quality, controller.fps)
        for _ in range(20):
            controller.observe(controller.interval * 0.5)
        self.assertEqual((controller.quality, controller.fps), before)

    def test_frame_interval_follows_fps(self):
        controller = QualityController(fps=5)
        self.assertAlmostEqual(controller.interval, 0.2)

    def test_fps_target_stays_in_the_requested_5_to_10_band(self):
        self.assertEqual((MIN_FPS, MAX_FPS), (5, 10))


class FrameBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_reader_gets_the_newest_frame_and_the_skip_count(self):
        broker = FrameBroker()
        broker.publish(JpegFrame(b"a", 1, 1))
        seq, frame, skipped = await broker.wait_newer(0)
        self.assertEqual((seq, frame.jpeg, skipped), (1, b"a", 0))
        # The reader is busy while three more frames arrive.
        for label in (b"b", b"c", b"d"):
            broker.publish(JpegFrame(label, 1, 1))
        seq, frame, skipped = await broker.wait_newer(seq)
        self.assertEqual((seq, frame.jpeg, skipped), (4, b"d", 2))

    async def test_reader_waits_until_a_frame_is_published(self):
        broker = FrameBroker()
        waiter = asyncio.create_task(broker.wait_newer(0))
        await asyncio.sleep(0.05)
        self.assertFalse(waiter.done())
        broker.publish(JpegFrame(b"x", 2, 2))
        seq, frame, _ = await asyncio.wait_for(waiter, 2)
        self.assertEqual((seq, frame.width), (1, 2))


class CaptureEncodingTests(unittest.TestCase):
    def test_capture_shrinks_to_the_width_limit_and_returns_jpeg_bytes(self):
        from PIL import Image

        big = Image.new("RGB", (2560, 1440), (10, 120, 200))
        with mock.patch("PIL.ImageGrab.grab", return_value=big):
            frame = stream.capture_jpeg(quality=60, max_width=1280)
        self.assertEqual((frame.width, frame.height), (1280, 720))
        self.assertEqual(frame.jpeg[:2], b"\xff\xd8")  # JPEG start-of-image marker
        decoded = Image.open(io.BytesIO(frame.jpeg))
        self.assertEqual(decoded.size, (1280, 720))

    def test_small_screens_are_never_enlarged(self):
        from PIL import Image

        small = Image.new("RGB", (800, 600), (0, 0, 0))
        with mock.patch("PIL.ImageGrab.grab", return_value=small):
            frame = stream.capture_jpeg(quality=60, max_width=1280)
        self.assertEqual((frame.width, frame.height), (800, 600))


if __name__ == "__main__":
    unittest.main()
