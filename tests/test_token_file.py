"""Token source precedence and token-file safety, using only temporary data."""
import contextlib
import io
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from assistant import audit, config as configmod
from assistant.config import ConfigError, load_config
from main import ensure_token
from build_zip import build_zip

FILE_TOKEN = '111111:AAHfile_value_abcdefghijk12345'
ENV_TOKEN = '222222:AAHenvironment_abcdefghijk1234'
STORE_TOKEN = '333333:AAHstored_value_abcdefghijk123'


class TokenFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = self.root / 'different' / 'config.ini'
        self.cfg.parent.mkdir()
        self.cfg.write_text('[telegram]\nallowed_user_ids = 42\nbot_token = 999999:AAHlegacy_value_1234567890123\n', encoding='utf-8')
        self.root_patch = patch.object(configmod, 'PROJECT_DIR', self.root)
        self.root_patch.start(); self.addCleanup(self.root_patch.stop)
        self.env_patch = patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': ENV_TOKEN}, clear=False)
        self.env_patch.start(); self.addCleanup(self.env_patch.stop)
        self.store_patch = patch('assistant.config.token_store.load_stored_token', return_value=STORE_TOKEN)
        self.store = self.store_patch.start(); self.addCleanup(self.store_patch.stop)
        self.path = self.root / 'token.txt'

    def test_file_wins_bom_whitespace_and_first_nonempty_line(self):
        contents = '\ufeff \t\n\n  \ufeff ' + FILE_TOKEN + '  \n' + ENV_TOKEN + '\n'
        self.path.write_text(contents, encoding='utf-8')
        loaded = load_config(self.cfg)
        self.assertEqual((loaded.bot_token, loaded.token_source), (FILE_TOKEN, 'file'))
        self.assertEqual(self.path.read_text(encoding='utf-8'), contents)
        self.store.assert_not_called()

    def test_environment_beats_store_when_file_missing_or_empty(self):
        self.assertEqual(load_config(self.cfg).token_source, 'env')
        self.path.write_text('\ufeff  \n\t\n', encoding='utf-8')
        loaded = load_config(self.cfg)
        self.assertEqual((loaded.bot_token, loaded.token_source), (ENV_TOKEN, 'env'))
        self.store.assert_not_called()

    def test_store_then_prompt_if_no_sources(self):
        os.environ.pop('TELEGRAM_BOT_TOKEN')
        self.assertEqual((load_config(self.cfg).bot_token, load_config(self.cfg).token_source), (STORE_TOKEN, 'store'))
        self.store.return_value = None
        loaded = load_config(self.cfg)
        self.assertEqual((loaded.bot_token, loaded.token_source), ('', ''))
        with patch('main.token_store.ask_for_token_interactively', return_value=STORE_TOKEN) as ask:
            self.assertTrue(ensure_token(loaded))
            ask.assert_called_once()
        self.assertEqual(loaded.bot_token, STORE_TOKEN)

    def test_no_prompt_when_file_loaded_and_no_delete(self):
        self.path.write_text(FILE_TOKEN, encoding='utf-8')
        loaded = load_config(self.cfg)
        with patch('main.token_store.ask_for_token_interactively') as ask:
            self.assertTrue(ensure_token(loaded))
            ask.assert_not_called()
        self.assertTrue(self.path.exists())

    def test_noninteractive_start_never_prompts(self):
        self.path.write_text('  \n', encoding='utf-8')
        os.environ.pop('TELEGRAM_BOT_TOKEN')
        self.store.return_value = None
        loaded = load_config(self.cfg)
        with patch('main.token_store.ask_for_token_interactively') as ask:
            self.assertFalse(ensure_token(loaded, allow_prompt=False))
            ask.assert_not_called()

    def test_unreadable_token_file_fails_closed(self):
        self.path.mkdir()
        with self.assertRaises(ConfigError):
            load_config(self.cfg)

    def test_invalid_nonempty_file_fails_closed_without_disclosure(self):
        self.path.write_text('bad_secret\n' + FILE_TOKEN, encoding='utf-8')
        with self.assertRaises(ConfigError) as error:
            load_config(self.cfg)
        self.assertNotIn('bad_secret', str(error.exception))
        self.store.assert_not_called()

    def test_invalid_encoding_fails_closed(self):
        self.path.write_bytes(b'\xff\xfe\x00')
        with self.assertRaises(ConfigError):
            load_config(self.cfg)

    def test_logging_formatter_scrubs_token_even_from_traceback(self):
        self.path.write_text(FILE_TOKEN, encoding='utf-8')
        load_config(self.cfg)
        formatter = audit.SafeFormatter('%(message)s')
        try:
            raise ValueError('oops ' + FILE_TOKEN)
        except ValueError:
            import sys
            record = logging.LogRecord('test', logging.ERROR, __file__, 1, 'token=' + FILE_TOKEN, (), sys.exc_info())
        self.assertNotIn(FILE_TOKEN, formatter.format(record))
        self.assertIn('[REDACTED]', formatter.format(record))

    def test_check_prints_no_token(self):
        from main import self_check
        self.path.write_text(FILE_TOKEN, encoding='utf-8')
        loaded = load_config(self.cfg)
        capture = io.StringIO()
        with patch('main.token_store.validate_token', return_value=(False, 'test error ' + FILE_TOKEN, None)), patch('main.build_planner') as planner, contextlib.redirect_stdout(capture):
            planner.return_value.status.return_value = {'mode': 'keywords', 'chain': ['keywords']}
            self_check(loaded)
        self.assertNotIn(FILE_TOKEN, capture.getvalue())

    def test_dry_run_does_not_print_token(self):
        from main import dry_run
        from assistant.planner import Plan, Step
        self.path.write_text(FILE_TOKEN, encoding='utf-8')
        loaded = load_config(self.cfg)
        plan = Plan(summary=FILE_TOKEN, steps=[Step('reply', {'text': FILE_TOKEN})])
        planner = type('Planner', (), {'plan': lambda self, request: plan})()
        output = io.StringIO()
        with patch('main.build_planner', return_value=planner), patch.dict(os.environ, {'ASSISTANT_PRINT_JSON': '1'}), contextlib.redirect_stdout(output):
            dry_run(loaded, FILE_TOKEN)
        self.assertNotIn(FILE_TOKEN, output.getvalue())
        self.assertIn('[REDACTED]', output.getvalue())

    def test_installer_does_not_prompt_when_file_exists(self):
        script = (Path(__file__).resolve().parent.parent / 'install.bat').read_text(encoding='utf-8')
        # The interactive prompt is a set /p read (EOF-safe; choice could hang
        # with stdin closed), and token.txt must short-circuit before it.
        prompt_marker = 'set /p "SAVE_TOKEN='
        self.assertLess(script.index('if exist "token.txt"'), script.index(prompt_marker))
        self.assertIn('goto after_token', script[script.index('if exist "token.txt"'):script.index(prompt_marker)])
        self.assertIn('main.py --no-token-prompt', script)

    def test_zip_builder_excludes_token_txt(self):
        import shutil
        root = self.root / 'project'
        shutil.copytree(Path(__file__).resolve().parent.parent, root, ignore=shutil.ignore_patterns('__pycache__', '.venv', 'token.txt', 'config.ini', 'logs', 'data', 'downloads', 'screenshots'))
        secret = '987654:AAHunique_' + os.urandom(24).hex()
        (root / 'token.txt').write_text(secret, encoding='utf-8')
        archive = self.root / 'out.zip'
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': ''}):
            build_zip(root, archive)
        with ZipFile(archive) as z:
            self.assertFalse(any(Path(name).name.casefold() == 'token.txt' for name in z.namelist()))
            self.assertFalse(any(secret.encode() in z.read(name) for name in z.namelist()))
