"""Round 3: private, confirmation-gated memory, voice, macros, briefing and cloud."""
import io
import json
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from assistant import audit, bot
from assistant.jobs import JobStore
from assistant.memory import MemoryStore, language_for
from assistant.planner import Plan, Step
from assistant.voice import transcribe, VoiceUnavailable
from assistant.actions import clear_abort
from test_flow import FakeMessage, make_config, make_context, make_update


class JarvisFlows(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = make_config(self.root)
        self.context = make_context(self.config)
        self.context.application.bot_data['jobs'] = JobStore(self.config.jobs_file)
        self.mem = MemoryStore(self.config.jobs_file.parent / 'memory.json')
        clear_abort(); self.addCleanup(clear_abort)

    def pending(self):
        return next(iter(self.context.application.bot_data['plans'].values()))

    async def approve(self, user_id=42):
        p = self.pending()
        msg = FakeMessage('approve ' + p.code)
        await bot.on_text(make_update(message=msg, user_id=user_id), self.context)
        return p

    async def run_command(self, func, args=(), user_id=42):
        self.context.args = list(args)
        msg = FakeMessage('/command ' + ' '.join(args))
        await func(make_update(message=msg, user_id=user_id), self.context)
        return msg

    async def test_note_write_read_and_forget_are_all_gated(self):
        await self.run_command(bot.cmd_note, ['hello', 'world'])
        self.assertFalse(self.mem.path.exists())
        self.assertEqual(self.pending().plan.steps[0].action, 'memory_update')
        await self.approve()
        self.assertEqual(self.mem.get(42)['notes'], ['hello world'])
        before = len(self.context.bot.messages)
        await self.run_command(bot.cmd_notes)
        self.assertEqual(self.pending().plan.steps[0].action, 'memory_read')
        self.assertFalse(any('hello world' in m for m in self.context.bot.messages[before:]))
        await self.approve()
        self.assertTrue(any('hello world' in m for m in self.context.bot.messages[before:]))
        await self.run_command(bot.cmd_forget, ['1'])
        self.assertEqual(self.mem.get(42)['notes'], ['hello world'])
        await self.approve()
        self.assertEqual(self.mem.get(42)['notes'], [])

    async def test_forget_refuses_changed_note(self):
        self.mem.mutate(42, 'add_note', 'old')
        await self.run_command(bot.cmd_forget, ['1'])
        self.mem.mutate(42, 'forget_note', '1', steps='old')
        self.mem.mutate(42, 'add_note', 'new')
        await self.approve()
        self.assertEqual(self.mem.get(42)['notes'], ['new'])
        self.assertTrue(any('changed' in m for m in self.context.bot.messages))

    async def test_preference_is_saved_only_after_approval(self):
        await self.run_command(bot.cmd_language, ['ar'])
        self.assertFalse(self.mem.path.exists())
        await self.approve()
        self.assertEqual(self.mem.get(42)['language'], 'ar')
        self.assertEqual(language_for('افتح كروم', 'en'), 'ar')
        self.assertEqual(language_for('open chrome', 'ar'), 'en')
        self.assertEqual(language_for('123', 'ar'), 'ar')
        await self.run_command(bot.cmd_briefing, ['now'])
        p = self.pending()
        self.assertEqual(p.plan.language, 'ar')
        self.assertIn('سيتم', bot.render_plan_message(p.plan, p.code, p.fingerprint))

    async def test_macro_save_and_run_requires_two_separate_approvals(self):
        a, b = self.root / 'a.txt', self.root / 'b.txt'
        original = Plan(summary='two files', steps=[Step('write_file', {'path':str(a), 'content':'A'}), Step('write_file', {'path':str(b), 'content':'B'})], planner='direct')
        await bot.propose_plan(make_update(), self.context, original)
        oldcode = self.pending().code
        await self.run_command(bot.cmd_macro, ['save', 'morning', 'routine'])
        self.assertNotEqual(oldcode, self.pending().code)
        self.assertFalse(self.mem.path.exists()); self.assertFalse(a.exists())
        await self.approve()
        self.assertFalse(a.exists()); self.assertEqual(len(self.mem.get(42)['macros']['morning routine']), 2)
        await self.run_command(bot.cmd_macro, ['run', 'morning', 'routine'])
        self.assertEqual(len(self.pending().plan.steps), 2)
        self.assertFalse(a.exists()); self.assertFalse(b.exists())
        await self.approve()
        self.assertEqual(a.read_text(encoding='utf-8'), 'A'); self.assertEqual(b.read_text(encoding='utf-8'), 'B')

    async def test_chain_proposes_multiple_steps_without_running(self):
        await self.run_command(bot.cmd_chain, ['open', 'chrome', ';', 'take', 'a', 'screenshot'])
        plan = self.pending().plan
        self.assertEqual([s.action for s in plan.steps], ['open', 'screenshot'])
        self.assertIn('Nothing has run yet', self.context.bot.messages[-1])

    async def test_chain_rejects_unrecognized_step(self):
        msg = await self.run_command(bot.cmd_chain, ['open', 'chrome', ';', 'gibberish'])
        self.assertFalse(self.context.application.bot_data['plans'])
        self.assertTrue(any('not understood' in x for x in msg.replies))

    async def test_macro_delete_requires_approval(self):
        self.mem.mutate(42, 'save_macro', name='morning', steps='[{"action":"screenshot","params":{}}]')
        await self.run_command(bot.cmd_macro, ['delete', 'morning'])
        self.assertIn('morning', self.mem.get(42)['macros'])
        await self.approve()
        self.assertNotIn('morning', self.mem.get(42)['macros'])

    async def test_macro_bad_data_is_never_executed(self):
        self.mem.mutate(42, 'save_macro', name='bad', steps=json.dumps([{'action':'mystery', 'params':{}}]))
        msg = await self.run_command(bot.cmd_macro, ['run', 'bad'])
        self.assertFalse(self.context.application.bot_data['plans'])
        self.assertTrue(any('invalid' in r for r in msg.replies))

    async def test_briefing_scheduled_gates_creation_and_each_delivery(self):
        from assistant.scheduler import process_due_jobs
        jobs = self.context.application.bot_data['jobs']
        await self.run_command(bot.cmd_briefing, ['daily', '09:00'])
        self.assertFalse(jobs.jobs)
        await self.approve()
        self.assertEqual(len(jobs.jobs), 1)
        job = jobs.jobs[0]
        job.due = '2000-01-01T09:00:00'; jobs.save()
        self.mem.mutate(42, 'add_note', 'briefing secret')
        before = len(self.context.bot.messages)
        self.context.application.bot = self.context.bot
        async def queue(j):
            return await bot.queue_briefing(self.context.application, j)
        self.assertEqual(await process_due_jobs(jobs, self.context.bot, self.config, queue), 1)
        self.assertEqual(self.pending().plan.steps[0].action, 'briefing')
        self.assertFalse(any('briefing secret' in m for m in self.context.bot.messages[before:]))
        await self.approve()
        self.assertTrue(any('briefing secret' in m for m in self.context.bot.messages[before:]))
        self.assertEqual(len(jobs.jobs), 1)
        self.assertEqual(jobs.jobs[0].repeat, 'daily')

    async def test_due_job_does_not_replace_pending_plan(self):
        from assistant.scheduler import process_due_jobs
        jobs = self.context.application.bot_data['jobs']
        job = jobs.add_briefing(42, '09:00'); job.due = '2000-01-01T09:00:00'
        await self.run_command(bot.cmd_note, ['pending'])
        original = self.pending()
        self.context.application.bot = self.context.bot
        async def queue(j): return await bot.queue_briefing(self.context.application, j)
        self.assertEqual(await process_due_jobs(jobs, self.context.bot, self.config, queue), 0)
        self.assertIs(self.pending(), original)
        self.assertIn(job, jobs.jobs)

    async def test_scheduled_command_needs_new_approval(self):
        from assistant.scheduler import process_due_jobs
        jobs = self.context.application.bot_data['jobs']
        job = jobs.add_task(42, 'Get-Date', '+0s')
        self.context.application.bot = self.context.bot
        async def queue(j): return await bot.queue_briefing(self.context.application, j)
        self.assertEqual(await process_due_jobs(jobs, self.context.bot, self.config, queue), 1)
        self.assertEqual(self.pending().plan.steps[0].action, 'powershell')
        self.assertEqual(self.pending().plan.steps[0].params['command'], job.command)

    async def test_external_approver_not_in_allowed_ids_is_rejected(self):
        from assistant.config import ConfigError
        with self.assertRaises(ConfigError):
            make_config(self.root, ids='42', approvers='43')

    async def test_forget_all_requires_approval(self):
        self.mem.mutate(42, 'add_note', 'private')
        self.mem.mutate(42, 'set_language', 'ar')
        await self.run_command(bot.cmd_forget, ['all'])
        self.assertTrue(self.mem.get(42).get('notes'))
        await self.approve()
        self.assertEqual(self.mem.get(42), {})

    async def test_denied_note_is_not_saved(self):
        await self.run_command(bot.cmd_note, ['not', 'saved'])
        code = self.pending().code
        await bot.on_text(make_update(message=FakeMessage('deny ' + code)), self.context)
        self.assertFalse(self.mem.path.exists())

    async def test_external_request_deny_makes_no_planner_call(self):
        class NeverPlanner:
            def plan(self, request): raise AssertionError('must not call')
        self.config.planner_mode = 'cloud'
        self.context.application.bot_data['planner'] = NeverPlanner()
        await bot.on_text(make_update(message=FakeMessage('open chrome')), self.context)
        code = self.pending().code
        await bot.on_text(make_update(message=FakeMessage('deny ' + code)), self.context)
        self.assertFalse(self.context.application.bot_data['plans'])

    async def test_unauthorized_and_group_requests_rejected(self):
        await self.run_command(bot.cmd_note, ['x'], user_id=999)
        msg = FakeMessage('/note group')
        update = make_update(message=msg)
        update.effective_chat.type = 'group'
        self.context.args = ['group']
        await bot.cmd_note(update, self.context)
        self.assertFalse(self.context.application.bot_data['plans'])
        self.assertFalse(self.mem.path.exists())

    async def test_cloud_consent_then_separate_pc_approval(self):
        class FakePlanner:
            def __init__(self): self.calls = []
            def plan(self, request):
                self.calls.append(request)
                return Plan(summary='write', steps=[Step('write_file', {'path':str(self.root/'cloud.txt'), 'content':'ok'})])
        planner = FakePlanner(); planner.root = self.root
        self.config.planner_mode = 'cloud'
        self.context.application.bot_data['planner'] = planner
        await bot.on_text(make_update(message=FakeMessage('write a file')), self.context)
        self.assertEqual(planner.calls, [])
        self.assertEqual(self.pending().plan.steps[0].action, 'cloud_plan')
        await self.approve()
        self.assertEqual(planner.calls, ['write a file'])
        self.assertFalse((self.root/'cloud.txt').exists())
        self.assertEqual(self.pending().plan.steps[0].action, 'write_file')
        await self.approve()
        self.assertEqual((self.root/'cloud.txt').read_text(encoding='utf-8'), 'ok')

    async def test_panic_discards_an_inflight_external_plan(self):
        import asyncio
        import threading
        started, release = threading.Event(), threading.Event()
        class SlowPlanner:
            def plan(self, request):
                started.set(); release.wait(timeout=3)
                return Plan(summary='danger', steps=[Step('lock_screen', {})])
        self.config.planner_mode = 'cloud'
        self.context.application.bot_data['planner'] = SlowPlanner()
        await bot.on_text(make_update(message=FakeMessage('lock the PC')), self.context)
        task = asyncio.create_task(self.approve())
        self.assertTrue(await asyncio.to_thread(started.wait, 3))
        await bot.cmd_panic(make_update(), self.context)
        release.set(); await task
        self.assertFalse(self.context.application.bot_data['plans'])

    async def test_raw_command_never_needs_cloud_planner(self):
        from assistant.planner import Planner
        self.config.planner_mode = 'cloud'
        planner = Planner.from_config(self.config)
        planner.cloud.plan = lambda *args: (_ for _ in ()).throw(AssertionError('network called'))
        self.context.application.bot_data['planner'] = planner
        await bot.on_text(make_update(message=FakeMessage('!Get-Date')), self.context)
        self.assertEqual(self.pending().plan.steps[0].action, 'powershell')

    async def test_remote_ollama_requires_data_transfer_approval(self):
        self.config.planner_mode = 'local'
        self.config.use_ollama = True
        self.config.ollama_base_url = 'https://ollama.example.test'
        await bot.on_text(make_update(message=FakeMessage('open chrome')), self.context)
        plan = self.pending().plan
        self.assertEqual(plan.steps[0].action, 'cloud_plan')
        self.assertIn('ollama.example.test', plan.steps[0].params['destination'])

    async def test_voice_uses_offline_planner_and_confirmation(self):
        self.config.planner_mode = 'cloud'
        class ForbiddenPlanner:
            def plan(self, request): raise AssertionError('cloud should not be called')
        self.context.application.bot_data['planner'] = ForbiddenPlanner()
        class TGFile:
            async def download_as_bytearray(self): return bytearray(b'voice')
        async def get_file(_): return TGFile()
        self.context.bot.get_file = get_file
        msg = FakeMessage(); msg.voice = SimpleNamespace(file_id='fid', file_size=5, duration=2)
        with patch('assistant.bot.transcribe', return_value='take a screenshot'):
            await bot.on_voice(make_update(message=msg), self.context)
        self.assertEqual(self.pending().plan.steps[0].action, 'screenshot')
        self.assertIn('take a screenshot', self.pending().plan.transcript)
        self.assertTrue(any('offline' in line for line in msg.replies))

    async def test_voice_missing_dependency_and_size_fail_gracefully(self):
        class TGFile:
            async def download_as_bytearray(self): return bytearray(b'voice')
        async def get_file(_): return TGFile()
        self.context.bot.get_file = get_file
        msg = FakeMessage(); msg.voice = SimpleNamespace(file_id='fid', file_size=5, duration=2)
        with patch('assistant.bot.transcribe', side_effect=VoiceUnavailable('Voice support is not installed.')):
            await bot.on_voice(make_update(message=msg), self.context)
        self.assertTrue(any('not installed' in x for x in msg.replies))
        self.assertFalse(self.context.application.bot_data['plans'])
        msg.voice.file_size = 9000000
        await bot.on_voice(make_update(message=msg), self.context)
        self.assertTrue(any('5 MB' in x for x in msg.replies))

    async def test_help_includes_features(self):
        msg = await self.run_command(bot.cmd_help)
        text = ' '.join(msg.replies)
        for command in ('/note', '/notes', '/forget', '/language', '/briefing', '/chain', '/macro', '/macros', '/panic', '/jobs', '/logs'):
            self.assertIn(command, text)


class VoiceAndAuditTests(unittest.TestCase):
    def test_faster_whisper_local_files_only_and_in_memory_audio(self):
        calls = {}
        class Model:
            def __init__(self, path, **kwargs): calls.update(kwargs)
            def transcribe(self, audio, **kwargs):
                self.assert_audio = audio
                calls['memory'] = isinstance(audio, io.BytesIO)
                return [SimpleNamespace(text=' hello ')], None
        module = types.ModuleType('faster_whisper'); module.WhisperModel = Model
        with tempfile.TemporaryDirectory() as path, patch.dict(sys.modules, {'faster_whisper':module}):
            self.assertEqual(transcribe(b'audio', Path(path)), 'hello')
        self.assertTrue(calls['local_files_only']); self.assertTrue(calls['memory'])

    def test_planner_request_never_follows_redirect_or_uses_proxy(self):
        from assistant.planner import LocalPlanner
        response = SimpleNamespace(raise_for_status=lambda: None,
            json=lambda: {'message': {'content': '{"summary":"x","steps":[{"action":"reply","params":{"text":"hi"}}]}'}})
        with patch('requests.post', return_value=response) as post:
            LocalPlanner('http://127.0.0.1:11434', 'example').plan('hello', Path('.'))
        self.assertEqual(post.call_args.kwargs['proxies'], {'http':'', 'https':''})
        self.assertFalse(post.call_args.kwargs['allow_redirects'])

    def test_audit_metadata_excludes_arbitrary_secrets(self):
        stream = io.StringIO(); handler = logging.StreamHandler(stream)
        logger = logging.getLogger('assistant'); logger.addHandler(handler)
        try:
            logger.setLevel(logging.INFO)
            audit.event('request', text='secret-password-xyz', user_id=42, params={'password':'secret-password-xyz'})
            audit.warning('secret-password-xyz', error='secret-password-xyz')
            audit.error('secret-password-xyz', exc_info=True)
        finally:
            logger.removeHandler(handler)
        self.assertNotIn('secret-password-xyz', stream.getvalue())
        self.assertIn('request', stream.getvalue())

class CancellationRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_panic_during_replanning_does_not_create_a_new_plan(self):
        import asyncio
        from test_flow import make_pending
        with tempfile.TemporaryDirectory() as folder:
            config = make_config(Path(folder))
            context = make_context(config)
            old = make_pending(Plan(summary='old', steps=[Step('lock_screen', {})]))
            context.application.bot_data['plans'][old.plan_id] = old
            started, release = asyncio.Event(), asyncio.Event()
            async def slow_mark(*args, **kwargs):
                started.set(); await release.wait()
            new = Plan(summary='new', steps=[Step('lock_screen', {})])
            with patch.object(bot, '_mark_messages', side_effect=slow_mark):
                task = asyncio.create_task(bot.propose_plan(make_update(), context, new))
                await started.wait()
                await bot.cmd_panic(make_update(), context)
                release.set()
                self.assertIsNone(await task)
            self.assertFalse(context.application.bot_data['plans'])
            clear_abort()

class ReleaseSafetyTests(unittest.TestCase):
    def test_release_builder_rejects_token_in_source_without_replacing_zip(self):
        import os
        import shutil
        from build_zip import build_zip
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'project'
            source = Path(__file__).resolve().parent.parent
            shutil.copytree(source, root, ignore=shutil.ignore_patterns('__pycache__', '.venv', 'token.txt', 'data', 'models', 'logs', 'downloads', 'screenshots'))
            token = '909090:AAHsecret_' + os.urandom(24).hex()
            (root / 'token.txt').write_text(token, encoding='utf-8')
            with (root / 'README.md').open('a', encoding='utf-8') as file:
                file.write(token)
            archive = Path(tmp) / 'release.zip'
            archive.write_bytes(b'old')
            with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': ''}):
                with self.assertRaises(ValueError) as error:
                    build_zip(root, archive)
            self.assertNotIn(token, str(error.exception))
            self.assertEqual(archive.read_bytes(), b'old')

    def test_voice_missing_dependency_is_clear_and_offline(self):
        with tempfile.TemporaryDirectory() as path, patch.dict(sys.modules, {'faster_whisper': None}):
            with self.assertRaises(VoiceUnavailable) as error:
                transcribe(b'voice', Path(path))
        self.assertIn('not installed', str(error.exception))
