"""Offline screen-sharing safety tests. No actual desktop or Telegram network is used."""
import asyncio
import base64
import io
import inspect
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import Bot
from telegram.error import RetryAfter
from assistant import bot, sharing
from assistant.actions import clear_abort, execute_step
from assistant.actions.base import ActionResult
from assistant.planner import Plan, Step
from test_flow import (FakeQuery, make_config, make_context, make_pending,
                       make_update, OWNER_ID)


class Indicator:
    def __init__(self, visible=True):
        self.alive = visible
        self.closed = False
    def visible(self):
        return self.alive
    def terminate_now(self):
        self.alive = False
    async def close(self):
        self.closed = True
        self.alive = False


class RecordingBot:
    def __init__(self):
        self.messages = []
        self.photos = []
        self.edits = []
        self.photo_event = asyncio.Event()
        self.edit_event = asyncio.Event()
        self.edit_error = None
        self.block_upload = None
    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))
    async def send_photo(self, chat_id, photo, caption):
        self.photos.append((chat_id, photo, photo.read(), caption))
        self.photo_event.set()
        if self.block_upload is not None:
            await self.block_upload.wait()
        return SimpleNamespace(message_id=len(self.photos) + 10)
    async def edit_message_media(self, chat_id, message_id, media):
        self.edits.append((chat_id, message_id, media, media.media.input_file_content))
        self.edit_event.set()
        if self.edit_error:
            raise self.edit_error


class ScreenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        clear_abort()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(clear_abort)
        self.cfg = make_config(Path(self.temp.name))
        self.context = make_context(self.cfg)
        self.update = make_update()
        self.ind = Indicator()
        self.rb = RecordingBot()
        self.captures = 0
        async def capture(owner, width, quality):
            self.captures += 1
            return b'jpeg'
        self.capture = capture
        async def indicator():
            return self.ind
        self.controller = sharing.WatchController(self.rb, self.cfg, indicator, capture)

    async def asyncTearDown(self):
        session = self.controller.active
        if session:
            self.controller.stop_now('test cleanup')
            await asyncio.gather(session.task, return_exceptions=True)

    async def begin(self):
        ok, msg = await self.controller.start(OWNER_ID, OWNER_ID, 2, 1)
        self.assertTrue(ok, msg)
        session = self.controller.active
        await asyncio.wait_for(self.rb.photo_event.wait(), 2)
        return session

    async def test_commands_propose_do_not_capture_before_approval(self):
        with patch.object(bot, '_run_visible_screenshot', new_callable=AsyncMock,
                          return_value=ActionResult('screenshot', True, 'sent')) as capture:
            await bot.cmd_screen(self.update, self.context)
            capture.assert_not_awaited()
            pending = next(iter(self.context.application.bot_data['plans'].values()))
            self.assertEqual(pending.plan.steps[0].action, 'screenshot')
            query = FakeQuery('apv:' + pending.plan_id, OWNER_ID)
            await bot.on_button(make_update(query=query), self.context)
            capture.assert_awaited_once()

    async def test_watch_and_window_are_plans_with_safe_defaults(self):
        self.context.args = ['start']
        await bot.cmd_watch(self.update, self.context)
        pending = next(iter(self.context.application.bot_data['plans'].values()))
        self.assertEqual(pending.plan.steps[0].params, {'seconds':'3', 'minutes':'10'})
        self.assertIsNone(self.context.application.bot_data.get('watch_controller'))
        self.context.args = ['My', 'Window']
        await bot.cmd_window(self.update, self.context)
        pending = next(iter(self.context.application.bot_data['plans'].values()))
        self.assertEqual(pending.plan.steps[0].params['title'], 'My Window')

    async def test_unauthorized_and_group_commands_are_dropped(self):
        for user, chat_type in [(999, 'private'), (OWNER_ID, 'group')]:
            update = make_update(user_id=user)
            update.effective_chat.type = chat_type
            await bot.cmd_screen(update, self.context)
        self.assertFalse(self.context.application.bot_data['plans'])
        self.assertEqual(self.context.bot.photos, 0)

    async def test_direct_action_without_indicator_is_denied(self):
        for name in ('screenshot', 'window_screenshot', 'watch_start'):
            result = execute_step(Step(name, {}), self.cfg)
            self.assertFalse(result.ok)

    async def test_one_shot_indicator_failure_and_private_routing(self):
        pending = make_pending(Plan('photo', [Step('screenshot', {})], planner='direct'))
        with patch.object(bot.ScreenIndicator, 'open', new=AsyncMock(side_effect=RuntimeError())), \
             patch.object(bot, 'capture_frame', new_callable=AsyncMock) as cap:
            result = await bot._run_visible_screenshot(pending.plan.steps[0], pending, self.context, self.cfg, None, None)
            self.assertFalse(result.ok)
            cap.assert_not_awaited()
        pending.chat_id = 999
        with patch.object(bot.ScreenIndicator, 'open', new_callable=AsyncMock) as opening:
            result = await bot._run_visible_screenshot(pending.plan.steps[0], pending, self.context, self.cfg, None, None)
            self.assertFalse(result.ok)
            opening.assert_not_awaited()

    async def test_one_shot_sends_only_allowed_photo_and_closes_stream(self):
        self.context.bot = self.rb
        pending = make_pending(Plan('photo', [Step('screenshot', {})], planner='direct'))
        with patch.object(bot.ScreenIndicator, 'open', new=AsyncMock(return_value=self.ind)), \
             patch.object(bot, 'capture_frame', new=AsyncMock(return_value=b'jpeg')):
            result = await bot._run_visible_screenshot(pending.plan.steps[0], pending, self.context, self.cfg, None, None)
        self.assertTrue(result.ok)
        self.assertIsNone(result.photo)
        self.assertEqual(self.rb.photos[0][0], OWNER_ID)
        self.assertTrue(self.rb.photos[0][1].closed)
        self.assertTrue(self.ind.closed)
        self.assertFalse((Path(self.temp.name) / 'shots').exists())

    async def test_panic_cancels_one_shot_in_flight(self):
        self.context.bot = self.rb
        self.rb.block_upload = asyncio.Event()
        pending = make_pending(Plan('photo', [Step('screenshot', {})], planner='direct'))
        with patch.object(bot.ScreenIndicator, 'open', new=AsyncMock(return_value=self.ind)), \
             patch.object(bot, 'capture_frame', new=AsyncMock(return_value=b'jpeg')):
            task = asyncio.create_task(bot._run_visible_screenshot(pending.plan.steps[0], pending, self.context, self.cfg, None, None))
            await asyncio.wait_for(self.rb.photo_event.wait(), 1)
            await bot.cmd_panic(self.update, self.context)
            result = await asyncio.wait_for(task, 1)
        self.assertFalse(result.ok)
        self.assertTrue(self.ind.closed)
        self.assertTrue(self.rb.photos[0][1].closed)

    async def test_start_restricts_private_allowed_and_limits(self):
        for args in [(999, 999, 3, 10), (OWNER_ID, -10, 3, 10),
                     (OWNER_ID, OWNER_ID, 1, 10), (OWNER_ID, OWNER_ID, 3, 61)]:
            ok, _ = await self.controller.start(*args)
            self.assertFalse(ok)
        self.assertEqual(self.captures, 0)

    async def test_indicator_failure_blocks_capture(self):
        self.ind.alive = False
        ok, _ = await self.controller.start(OWNER_ID, OWNER_ID, 3, 1)
        self.assertFalse(ok)
        self.assertEqual(self.captures, 0)
        self.assertFalse(self.rb.photos)

    async def test_watch_sends_photo_then_edits_and_closes_buffers(self):
        session = await self.begin()
        session.interval = .03  # test clock speed only; public API requires >=2
        await asyncio.wait_for(self.rb.edit_event.wait(), 3)
        self.assertEqual(len(self.rb.photos), 1)
        self.assertEqual(self.rb.edits[0][1], 11)
        self.assertEqual(self.rb.edits[0][3], b'jpeg')
        self.assertTrue(self.rb.photos[0][1].closed)
        self.assertEqual(self.rb.edits[0][2].media.filename, 'screen.jpg')
        self.controller.stop_now('requested')
        await session.task
        self.assertTrue(any('started' in m[1] for m in self.rb.messages))
        self.assertTrue(any('stopped' in m[1] for m in self.rb.messages))

    async def test_stop_cancels_upload_and_frames_do_not_overlap(self):
        self.rb.block_upload = asyncio.Event()
        session = await self.begin()
        session.interval = .02
        await asyncio.sleep(.1)
        self.assertEqual(self.captures, 1)
        self.assertTrue(self.controller.stop_now('requested'))
        await asyncio.wait_for(session.task, 1)
        self.assertTrue(self.rb.photos[0][1].closed)
        self.assertTrue(self.ind.closed)

    async def test_indicator_loss_cancels_inflight_upload(self):
        self.rb.block_upload = asyncio.Event()
        session = await self.begin()
        self.ind.alive = False
        await asyncio.wait_for(session.task, 1)
        self.assertEqual(self.captures, 1)
        self.assertTrue(self.rb.photos[0][1].closed)
        self.assertIn('indicator', self.rb.messages[-1][1])

    async def test_edit_failure_falls_back_but_is_throttled(self):
        self.rb.edit_error = RuntimeError('cannot edit')
        session = await self.begin()
        session.interval = .02
        await asyncio.wait_for(self.rb.edit_event.wait(), 3)
        await asyncio.sleep(.13)
        self.assertEqual(len(self.rb.photos), 2)
        self.assertGreaterEqual(len(self.rb.edits), 2)

    async def test_unchanged_edit_does_not_fallback(self):
        self.rb.edit_error = RuntimeError('Message is not modified')
        session = await self.begin()
        session.interval = .02
        await asyncio.wait_for(self.rb.edit_event.wait(), 3)
        await asyncio.sleep(.05)
        self.assertEqual(len(self.rb.photos), 1)

    async def test_retry_after_backs_off_without_fallback(self):
        self.rb.edit_error = RetryAfter(5)
        session = await self.begin()
        session.interval = .02
        with patch.object(sharing, '_seconds', return_value=.2):
            await asyncio.wait_for(self.rb.edit_event.wait(), 3)
            before = self.captures
            await asyncio.sleep(.09)
            self.assertEqual(self.captures, before)
            self.assertEqual(len(self.rb.photos), 1)
        self.assertEqual(sharing._seconds(RetryAfter(timedelta(seconds=7))), 7)

    async def test_speed_session_bound_and_timeout(self):
        session = await self.begin()
        self.assertFalse(self.controller.speed(OWNER_ID, session.id, 1)[0])
        self.assertFalse(self.controller.speed(999, session.id, 3)[0])
        self.assertFalse(self.controller.speed(OWNER_ID, 'old', 3)[0])
        self.assertTrue(self.controller.speed(OWNER_ID, session.id, 4)[0])
        self.assertEqual(session.interval, 4)
        self.controller.stop_now('requested')
        await session.task
        self.ind = Indicator()
        s = sharing.WatchSession(self.controller, OWNER_ID, OWNER_ID, 2, .001)
        self.controller.active = s
        s.task = asyncio.create_task(s._run())
        await asyncio.wait_for(s.task, 1)
        self.assertEqual(s.reason, 'time limit reached')
        self.assertIsNone(self.controller.active)

    async def test_panic_stops_watch_and_cancels_active_upload(self):
        self.rb.block_upload = asyncio.Event()
        session = await self.begin()
        self.context.application.bot_data['watch_controller'] = self.controller
        await bot.cmd_panic(self.update, self.context)
        await asyncio.wait_for(session.task, 1)
        self.assertTrue(self.ind.closed)
        self.assertTrue(self.rb.photos[0][1].closed)
        self.assertEqual(self.captures, 1)

    async def test_window_failure_never_calls_desktop_capture(self):
        pending = make_pending(Plan('photo', [Step('window_screenshot', {'title':'missing'})], planner='direct'))
        with patch.object(bot.ScreenIndicator, 'open', new=AsyncMock(return_value=self.ind)), \
             patch.object(bot, 'capture_frame', new=AsyncMock(side_effect=RuntimeError())) as capture:
            result = await bot._run_visible_screenshot(pending.plan.steps[0], pending, self.context, self.cfg, None, None)
        self.assertFalse(result.ok)
        self.assertEqual(capture.await_count, 1)
        self.assertEqual(capture.await_args.args[3], 'missing')
        self.assertEqual(self.context.bot.photos, 0)

    async def test_stop_invalidates_pending_watch_plan(self):
        self.context.args = ['start']
        await bot.cmd_watch(self.update, self.context)
        self.context.args = ['stop']
        await bot.cmd_watch(self.update, self.context)
        self.assertFalse(self.context.application.bot_data['plans'])
        self.assertEqual(self.context.application.bot_data['watch_epoch'], 1)

    async def test_window_capture_decodes_untruncated_memory_and_escapes_title(self):
        from PIL import Image
        png = io.BytesIO()
        Image.new('RGB', (60, 40), 'red').save(png, format='PNG')
        output = base64.b64encode(png.getvalue())
        class Process:
            returncode = 0
            async def communicate(self):
                return output, b''
        title = "evil'; Stop-Process; #"
        lease = sharing.CaptureLease()
        lease.indicator = self.ind
        with patch('assistant.actions.base.powershell_exe', return_value=['powershell', '-Command']), \
             patch.object(sharing, '_spawn', new=AsyncMock(return_value=Process())) as spawn:
            jpeg = await sharing.capture_frame(lease, 1280, 70, title, 123)
        args = spawn.await_args.args
        self.assertNotIn(title, args[-1])
        self.assertIn(base64.b64encode(title.encode()).decode(), args[-1])
        with Image.open(io.BytesIO(jpeg)) as image:
            self.assertEqual(image.format, 'JPEG')
            self.assertEqual(image.size, (60, 40))

    async def test_stop_during_indicator_start_does_not_orphan_session(self):
        gate = asyncio.Event()
        async def slow_indicator():
            await gate.wait()
            return self.ind
        self.controller.indicator_factory = slow_indicator
        starting = asyncio.create_task(self.controller.start(OWNER_ID, OWNER_ID, 3, 1))
        await asyncio.sleep(0)
        session = self.controller.active
        self.controller.stop_now('requested')
        ok, _ = await asyncio.wait_for(starting, 1)
        self.assertFalse(ok)
        await asyncio.gather(session.task, return_exceptions=True)
        await asyncio.sleep(0)
        self.assertIsNone(self.controller.active)
        self.assertFalse(self.rb.photos)

    async def test_telegram_api_keyword_signatures(self):
        inspect.signature(Bot.send_photo).bind(None, chat_id=1, photo=b'x', caption='x')
        inspect.signature(Bot.edit_message_media).bind(None, chat_id=1, message_id=1, media=None)
