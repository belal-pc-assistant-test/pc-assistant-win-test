"""Offline action and safety tests. OS effects are mocked."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from assistant.config import normalize_path
from test_assistant import make_config
from test_flow import FakeTransport, FakeQuery, FakeMessage, make_context, make_update, make_pending
from assistant import bot
from assistant.action_spec import VALID_ACTIONS, ACTION_RISK, overall_risk, risk_of
from assistant.actions import execute_step, clear_abort
from assistant.actions.dispatch import HANDLERS
from assistant.actions import system
from assistant.jobs import JobStore, parse_when
from assistant.plan_code import fingerprint, normalize_code, unique_code
from assistant.planner import Step, Plan


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = make_config(self.root)
        self.config.allowed_roots = [self.root]
        self.config.downloads_dir = self.root / 'downloads'
        clear_abort()
        self.addCleanup(clear_abort)

    def run_action(self, name, params=None, **kwargs):
        return execute_step(Step(name, params or {}), self.config, **kwargs)

    def test_registry_and_risks(self):
        self.assertEqual(set(HANDLERS), set(VALID_ACTIONS))
        self.assertEqual(set(ACTION_RISK), set(VALID_ACTIONS))
        self.assertEqual(risk_of('scheduled_task'), 'high')
        self.assertEqual(overall_risk(['system_info', 'powershell']), 'high')

    def test_system_info_and_process_listing(self):
        # Real PowerShell on Windows can take >20 s on a cold CI runner (CIM
        # queries); make_config now allows 60 s so this is not a timeout test.
        self.assertTrue(self.run_action('system_info').ok,
                        getattr(self.run_action('system_info'), 'output', ''))
        for sort in ('cpu', 'memory', 'name'):
            result = self.run_action('list_processes', {'sort': sort})
            self.assertTrue(result.ok, result.output)
        self.assertFalse(self.run_action('list_processes', {'sort': 'bad'}).ok)

    def test_protected_processes(self):
        for params in ({'pid':'1'}, {'pid':str(os.getpid())}, {'name':'lsass.exe'}):
            result = self.run_action('kill_process', params)
            self.assertFalse(result.ok, result.output)
        self.config.protected_processes = ['myservice']
        self.assertFalse(self.run_action('kill_process', {'name':'myservice.exe'}).ok)
        with patch.object(system, 'IS_WINDOWS', True), patch.object(system, 'run_powershell', return_value=SimpleNamespace(returncode=0, stdout='lsass', stderr='')) as runner:
            result = self.run_action('kill_process', {'pid':'98765'})
            self.assertFalse(result.ok)
            self.assertEqual(runner.call_count, 1)

    def test_volume_lock_and_notification(self):
        proc = SimpleNamespace(returncode=0, stdout='', stderr='')
        with patch.object(system, 'IS_WINDOWS', True), patch.object(system, 'run_powershell', return_value=proc) as ps, patch.object(system, 'run_process', return_value=proc):
            self.assertTrue(self.run_action('volume', {'level':'32'}).ok)
            self.assertFalse(self.run_action('volume', {'level':'101'}).ok)
            self.assertTrue(self.run_action('lock_screen').ok)
            payload = "'@\nRemove-Item x\n#"
            self.assertTrue(self.run_action('notification', {'text':payload}).ok)
            self.assertNotIn(payload, ps.call_args.args[0])

    def test_volume_requires_explicit_mode(self):
        """An unknown or missing mode must change nothing (no mute fallback)."""
        proc = SimpleNamespace(returncode=0, stdout='', stderr='')
        with patch.object(system, 'IS_WINDOWS', True), \
             patch.object(system, 'run_powershell', return_value=proc) as ps:
            for params in ({}, {'mode': ''}, {'mode': '   '}, {'mode': 'louder'},
                           {'mode': 'MUTE_MUSIC'}, {'mode': 'set'}):
                result = self.run_action('volume', params)
                self.assertFalse(result.ok, (params, result.output))
                self.assertIn('mode', result.output.lower())
                self.assertIn('nothing was changed', result.output.lower())
            ps.assert_not_called()  # no key press may happen for bad modes
        with patch.object(system, 'IS_WINDOWS', True), \
             patch.object(system, 'run_powershell', return_value=proc) as ps:
            for params in ({'mode': 'mute'}, {'mode': 'unmute'}, {'mode': 'toggle'},
                           {'mode': 'up', 'steps': '3'}, {'mode': 'down'},
                           {'mode': 'set', 'level': '40'}):
                self.assertTrue(self.run_action('volume', params).ok, params)
            self.assertEqual(ps.call_count, 6)

    def test_volume_key_sequence_unknown_mode_raises(self):
        with self.assertRaises(Exception):
            system.volume_key_sequence('louder', None, 5)
        self.assertEqual(system.volume_key_sequence('mute', None, 1), ['mute'])
        self.assertEqual(system.volume_key_sequence('up', None, 3), ['up'] * 3)
        self.assertEqual(system.volume_key_sequence('set', 50, 5)[-1], 'up')

    def test_clipboard(self):
        with patch.object(system, '_clipboard_read', return_value='secret'), patch.object(system, '_clipboard_write') as write:
            self.assertIn('secret', self.run_action('clipboard_get').output)
            self.assertTrue(self.run_action('clipboard_set', {'text':'hello'}).ok)
            write.assert_called_once_with('hello')

    def test_send_file_allowlist_and_size(self):
        file = self.root / 'doc.txt'; file.write_bytes(b'abc')
        result = self.run_action('send_file', {'path':str(file)})
        self.assertTrue(result.ok, result.output)
        # The action returns the normalized path (8.3 short names expanded on
        # Windows), so compare in normalized form instead of the raw literal.
        self.assertEqual(result.files, [normalize_path(file)])
        self.assertFalse(self.run_action('send_file', {'path':str(self.root.parent / 'outside')}).ok)
        self.config.max_send_file_mb = 0
        self.assertFalse(self.run_action('send_file', {'path':str(file)}).ok)

    def test_receive_file_and_cleanup(self):
        result = self.run_action('receive_file', {'file_id':'abc', 'file_name':'../report.txt'}, transport=FakeTransport(b'hello'))
        self.assertTrue(result.ok, result.output)
        self.assertEqual((self.config.downloads_dir / 'report.txt').read_bytes(), b'hello')
        result = self.run_action('receive_file', {'file_id':'abc', 'file_name':'report.txt'}, transport=FakeTransport(b'next'))
        self.assertTrue(result.ok); self.assertTrue((self.config.downloads_dir / 'report (1).txt').exists())
        self.config.max_send_file_mb = 0
        result = self.run_action('receive_file', {'file_id':'abc', 'file_name':'big.bin'}, transport=FakeTransport(b'bad'))
        self.assertFalse(result.ok); self.assertFalse(list(self.config.downloads_dir.glob('*.part')))

    def test_find_files_and_url_guard(self):
        (self.root / 'example.pdf').write_text('ok')
        self.assertIn('example.pdf', self.run_action('find_files', {'ext':'pdf'}).output)
        self.assertFalse(self.run_action('find_files', {'root':str(self.root.parent)}).ok)
        with patch('assistant.actions.core._open_external') as op:
            self.assertFalse(self.run_action('open_url', {'url':'file:///etc/passwd'}).ok)
            self.assertTrue(self.run_action('open_url', {'url':'https://example.com'}).ok)
            op.assert_called_once()

    def test_window_screenshot_fails_without_full_screen_fallback(self):
        proc = SimpleNamespace(returncode=1, stdout='', stderr='no window')
        with patch.object(system, 'IS_WINDOWS', True), patch.object(system, 'run_powershell', return_value=proc), patch('assistant.actions.core.capture_desktop_jpeg') as capture:
            result = self.run_action('window_screenshot', {'title':'missing'}, capture_allowed=True)
            self.assertFalse(result.ok)
            self.assertIn('no full-screen fallback', result.output)
            capture.assert_not_called()

    def test_jobs_persist(self):
        store = JobStore(self.root / 'jobs.json')
        self.assertTrue(self.run_action('reminder', {'text':'tea', 'when':'+10m'}, jobs=store, chat_id=42).ok)
        self.assertTrue(self.run_action('scheduled_task', {'command':'Get-Date', 'when':'+2h', 'repeat':'daily'}, jobs=store, chat_id=42).ok)
        self.assertEqual(len(JobStore(store.path).all()), 2)
        self.assertFalse(self.run_action('scheduled_task', {'command':'x','when':'+1h','repeat':'weekly'}, jobs=store, chat_id=42).ok)

    def test_power_modes_delay(self):
        proc = SimpleNamespace(returncode=0, stdout='', stderr='')
        with patch.object(system, 'IS_WINDOWS', True), patch.object(system, 'run_process', return_value=proc) as run:
            for mode in ('shutdown','restart','sleep','hibernate','cancel'):
                self.assertTrue(self.run_action('power', {'mode':mode, 'delay':'1'}).ok)
                if mode in ('shutdown','restart'):
                    self.assertEqual(run.call_args.args[0][-1], '30')
            self.assertFalse(self.run_action('power', {'mode':'invalid'}).ok)


class UtilityTests(unittest.TestCase):
    def test_parse_when(self):
        now = datetime(2026, 1, 1, 12, 0)
        self.assertEqual(parse_when('+10m', now), now + timedelta(minutes=10))
        self.assertEqual(parse_when('بعد 10 دقائق', now), now + timedelta(minutes=10))
        self.assertEqual(parse_when('11:00', now), datetime(2026,1,2,11))
        self.assertEqual(parse_when('2026-03-01 14:30', now), datetime(2026,3,1,14,30))
        with self.assertRaises(ValueError): parse_when('25:00', now)

    def test_fingerprint_and_codes(self):
        a = Step('write_file', {'path':'x', 'content':'hi'})
        b = Step('write_file', {'content':'hi', 'path':'x'})
        self.assertEqual(fingerprint([a]), fingerprint([b]))
        b.params['content'] = 'bye'
        self.assertNotEqual(fingerprint([a]), fingerprint([b]))
        self.assertEqual(normalize_code(' ab-cd '), 'ABCD')
        with patch('assistant.plan_code.generate_code', side_effect=['AAAAAA','BBBBBB']):
            self.assertEqual(unique_code(['aaaaaa']), 'BBBBBB')


class SeparationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_flow import make_config as flow_config
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = flow_config(self.root, ids='42,43', approvers='43', distinct=True)
        self.context = make_context(self.config)
        self.path = self.root / 'confirmed.txt'
        plan = Plan(summary='write', steps=[Step('write_file', {'path':str(self.path), 'content':'ok'})], planner='direct')
        self.pending = make_pending(plan)
        self.context.application.bot_data['plans'][self.pending.plan_id] = self.pending
        clear_abort(); self.addCleanup(clear_abort)

    async def test_external_approver_can_use_code(self):
        msg = FakeMessage('approve ' + self.pending.code)
        await bot.on_text(make_update(message=msg, user_id=43), self.context)
        self.assertEqual(self.path.read_text(), 'ok')

    async def test_external_approver_can_use_button(self):
        query = FakeQuery('apv:' + self.pending.plan_id, 43)
        await bot.on_button(make_update(query=query, user_id=43), self.context)
        self.assertTrue(self.path.exists())

    async def test_button_rejects_modified_plan(self):
        self.pending.plan.steps[0].params['content'] = 'evil'
        query = FakeQuery('apv:' + self.pending.plan_id, 43)
        await bot.on_button(make_update(query=query, user_id=43), self.context)
        self.assertFalse(self.path.exists())
        self.assertTrue(any('fingerprint' in x for x in query.edits))

    async def test_unlisted_user_cannot_request(self):
        message = FakeMessage('open chrome')
        await bot.on_text(make_update(message=message, user_id=999), self.context)
        self.assertFalse(message.replies)

    async def test_requester_cannot_self_approve_in_two_person_mode(self):
        self.config.allowed_user_ids = frozenset({42, 43})
        self.config.approver_user_ids = frozenset({42, 43})
        self.config.require_distinct_approver = True
        message = FakeMessage('approve ' + self.pending.code)
        await bot.on_text(make_update(message=message, user_id=42), self.context)
        self.assertFalse(self.path.exists())
        self.assertTrue(any('different' in x for x in message.replies))

    async def test_oversized_plan_refused_without_code(self):
        plan = Plan(summary='large', steps=[Step('write_file', {'path':str(self.path), 'content':'z'*6000})], planner='direct')
        message = FakeMessage()
        result = await bot.propose_plan(make_update(message=message), self.context, plan)
        self.assertIsNone(result)
        self.assertEqual(len(self.context.application.bot_data['plans']), 0)
        self.assertTrue(any('too long' in x for x in message.replies))

    async def test_panic_interrupts_running_plan(self):
        import asyncio
        from unittest.mock import patch
        from assistant.actions.base import ActionResult
        started, release = asyncio.Event(), asyncio.Event()
        async def slow_mark(*args, **kwargs):
            started.set()
            await release.wait()
        second = self.root / 'should_not_exist'
        self.pending.plan.steps.append(Step('write_file', {'path':str(second), 'content':'x'}))
        self.pending.fingerprint = fingerprint(self.pending.plan.steps)
        with patch.object(bot, '_mark_messages', side_effect=slow_mark), patch.object(bot, 'execute_step', return_value=ActionResult('write_file', True, 'ok')) as action:
            task = asyncio.create_task(bot._run_plan(make_update(), self.context, self.pending, 43, 'test'))
            await started.wait()
            await bot.cmd_panic(make_update(user_id=43), self.context)
            release.set()
            await task
            self.assertEqual(action.call_count, 0)

    async def test_send_file_never_deletes_user_image(self):
        self.config.keep_screenshots = False
        photo = self.root / 'myphoto.png'; photo.write_bytes(b'png')
        await bot._send_file(self.context.bot, 42, photo, True, self.config)
        self.assertTrue(photo.exists())
        await bot._send_file(self.context.bot, 42, photo, True, self.config, is_screenshot=True)
        self.assertFalse(photo.exists())

class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = make_config(self.root)
        self.store = JobStore(self.root / 'jobs.json')
        self.sent = []
        async def send_message(**kwargs):
            self.sent.append(kwargs)
        self.bot = SimpleNamespace(send_message=send_message)
        clear_abort(); self.addCleanup(clear_abort)

    async def test_due_reminder_is_queued_without_delivery(self):
        from assistant.scheduler import process_due_jobs
        self.store.add_reminder(42, 'hello', '+0s')
        queued = []
        async def queue(job):
            queued.append(job)
            return True
        self.assertEqual(await process_due_jobs(self.store, self.bot, self.config, queue), 1)
        self.assertEqual(queued[0].text, 'hello')
        self.assertFalse(self.sent)
        self.assertFalse(JobStore(self.store.path).jobs)

    async def test_due_jobs_wait_when_panic_is_active(self):
        from assistant.actions import request_abort
        from assistant.scheduler import process_due_jobs
        self.store.add_reminder(42, 'later', '+0s')
        request_abort()
        self.assertEqual(await process_due_jobs(self.store, self.bot, self.config), 0)
        self.assertEqual(len(self.store.jobs), 1)

    async def test_scheduled_command_is_not_run_by_scheduler(self):
        from assistant.scheduler import process_due_jobs
        self.store.add_task(42, 'Write-Output "<x>"', '+0s')
        queued = []
        async def queue(job):
            queued.append(job.command)
            return True
        self.assertEqual(await process_due_jobs(self.store, self.bot, self.config, queue), 1)
        self.assertEqual(queued, ['Write-Output "<x>"'])
        self.assertFalse(self.sent)

class PlanningCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_panic_cancels_inflight_planning(self):
        import asyncio
        import threading
        from test_flow import make_config as flow_config
        with tempfile.TemporaryDirectory() as tmp:
            config = flow_config(Path(tmp))
            started, release = threading.Event(), threading.Event()
            class SlowPlanner:
                def plan(self, request):
                    started.set()
                    release.wait(timeout=3)
                    return Plan(summary='test', steps=[Step('lock_screen', {})], planner='keywords')
            context = make_context(config, planner=SlowPlanner())
            message = FakeMessage('lock screen')
            task = asyncio.create_task(bot.on_text(make_update(message=message), context))
            await asyncio.to_thread(started.wait, 3)
            await bot.cmd_panic(make_update(), context)
            release.set()
            await task
            self.assertFalse(context.application.bot_data['plans'])
            self.assertTrue(any('cancelled by /panic' in text for text in message.replies))
            clear_abort()
