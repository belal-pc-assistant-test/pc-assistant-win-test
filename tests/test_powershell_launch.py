"""Round 9: how the bot starts Windows PowerShell.

Windows CI found that a child Windows PowerShell started from a PowerShell 7 parent
inherits ``PSModulePath`` and then cannot load ``Get-Acl``. These tests stub
``subprocess`` and check what the bot would pass to it: the executable and the
environment. They run on Linux. The real-Windows reproduction is in test_windows_real.py.
"""
from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _hermetic import VIOLATIONS, ForbiddenCall, IsolatedCase, ModuleGuard, release_log_then_remove  # noqa: E402
from assistant import audit, token_store, winshell  # noqa: E402
from assistant.actions import base, system  # noqa: E402
from assistant.actions.base import ActionError  # noqa: E402

# Any real process or network call in this module fails the test (see tests/_hermetic.py).
_guard = ModuleGuard()


def setUpModule():
    _guard.start()


def tearDownModule():
    _guard.stop()

PS7_MODULE_PATH = r"C:\Program Files\PowerShell\7\Modules;C:\Program Files\WindowsPowerShell\Modules"
SID = "S-1-5-21-111-222-333-1001"
FAKE_TOKEN = "123456789:AAHsecretTokenValueForRedactionTests_abcdef"
CLIXML_ERROR = (
    "#< CLIXML\n"
    '<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">'
    '<Obj S="progress" RefId="0"><TN RefId="0"><T>System.Management.Automation.PSCustomObject</T></TN>'
    '<MS><I64 N="SourceId">1</I64><PR N="Record"><AV>Preparing modules for first use.</AV></PR></MS></Obj>'
    '<S S="Error">Get-Acl : The command was found in module Microsoft.PowerShell.Security '
    'but the module could not be loaded._x000D__x000A_ '
    'At C:\\Users\\runneradmin\\AppData\\Local\\Temp\\tpa\\x.ps1:1 char:1</S>'
    "</Objs>"
)


def _proc(returncode=0, stdout=b"", stderr=b""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class PowerShellPathTests(IsolatedCase):

    def test_full_path_under_systemroot_when_the_file_exists(self):
        with tempfile.TemporaryDirectory() as root:
            exe = Path(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"")
            self.assertEqual(winshell.windows_powershell_path({"SystemRoot": root}), str(exe))

    def test_bare_name_only_when_the_file_is_missing(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(winshell.windows_powershell_path({"SystemRoot": root}), "powershell")
        self.assertEqual(winshell.windows_powershell_path({}), "powershell")

    def test_windir_is_used_when_systemroot_is_absent(self):
        with tempfile.TemporaryDirectory() as root:
            exe = Path(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"")
            self.assertEqual(winshell.windows_powershell_path({"WINDIR": root}), str(exe))


class ChildEnvironmentTests(IsolatedCase):

    def test_powershell7_module_settings_are_removed_in_any_case(self):
        source = {
            "PATH": r"C:\Windows", "SystemRoot": r"C:\Windows",
            "PSModulePath": PS7_MODULE_PATH, "psmodulepath": PS7_MODULE_PATH,
            "PSExecutionPolicyPreference": "Bypass", "psexecutionpolicypreference": "Bypass",
        }
        env = winshell.child_env(source)
        self.assertEqual(env, {"PATH": r"C:\Windows", "SystemRoot": r"C:\Windows"})

    def test_the_parent_environment_is_not_changed(self):
        with mock.patch.dict(os.environ, {"PSModulePath": PS7_MODULE_PATH}):
            winshell.child_env()
            self.assertEqual(os.environ["PSModulePath"], PS7_MODULE_PATH)


class DecodeAndSanitizeTests(IsolatedCase):

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(release_log_then_remove, self.tmp)
        audit.setup(Path(self.tmp.name) / "a.log", "CRITICAL")

    def test_decode_accepts_text_bytes_and_nothing(self):
        self.assertEqual(winshell.decode_output("text"), "text")
        self.assertEqual(winshell.decode_output("مرحبا".encode("utf-8")), "مرحبا")
        self.assertEqual(winshell.decode_output(b"\xff\xfe bad"), "\ufffd\ufffd bad")
        self.assertEqual(winshell.decode_output(None), "")
        self.assertEqual(winshell.decode_output(object()), "")

    def test_clixml_keeps_only_the_error_text(self):
        reason = winshell.sanitize_failure(CLIXML_ERROR)
        self.assertIn("could not be loaded", reason)
        self.assertNotIn("Preparing modules", reason)
        self.assertNotIn("CLIXML", reason)
        self.assertNotIn("_x000D_", reason)

    def test_paths_are_replaced_in_reasons(self):
        reason = winshell.sanitize_failure(CLIXML_ERROR)
        self.assertNotIn("runneradmin", reason)
        self.assertNotIn("C:\\", reason)
        self.assertIn("<path>", reason)
        posix = winshell.sanitize_failure("failed reading /home/user/private/notes.txt now")
        self.assertNotIn("/home/user", posix)

    def test_token_shaped_strings_and_registered_secrets_never_appear(self):
        audit.register_secret(FAKE_TOKEN)
        text = f"error while using {FAKE_TOKEN} and 987654321:AAHanotherLookingTokenABCDEFGHIJKL"
        reason = winshell.sanitize_failure(text)
        self.assertNotIn(FAKE_TOKEN, reason)
        self.assertNotIn("AAHanotherLookingToken", reason)
        self.assertIn("[REDACTED]", reason)

    def test_reason_is_short_one_line_and_never_empty(self):
        long_text = "x " * 500
        reason = winshell.sanitize_failure(long_text, limit=60)
        self.assertLessEqual(len(reason), 60)
        self.assertTrue(reason.endswith("…"))
        self.assertNotIn("\n", reason)
        self.assertEqual(winshell.sanitize_failure(""), "PowerShell gave no details")
        self.assertEqual(winshell.sanitize_failure(None), "PowerShell gave no details")
        progress_only = ("#< CLIXML\n<Objs><Obj S=\"progress\"><MS><PR N=\"Record\">"
                         "<AV>Preparing modules</AV></PR></MS></Obj></Objs>")
        self.assertEqual(winshell.sanitize_failure(progress_only), "PowerShell gave no details")

    def test_user_typed_path_is_shown_as_typed_and_other_paths_are_not(self):
        typed = "C:\\Users\\me\\My Docs\\report.pdf"
        error = f"Cannot find {typed} because C:\\Users\\RUNNER~1\\AppData\\x.txt is locked"
        reason = winshell.sanitize_failure(error, keep=[typed])
        self.assertIn(typed, reason)
        self.assertNotIn("RUNNER", reason)
        self.assertNotIn("AppData", reason)

    def test_a_folder_name_with_spaces_leaves_no_tail_behind(self):
        reason = winshell.sanitize_failure("denied: C:\\Users\\me\\My Docs\\a.txt")
        self.assertIn("<path>", reason)
        self.assertNotIn("Docs", reason)
        self.assertNotIn("a.txt", reason)

    def test_a_token_inside_the_kept_value_is_still_redacted(self):
        typed = f"C:\\Users\\me\\{FAKE_TOKEN}\\doc.txt"
        reason = winshell.sanitize_failure(f"missing {typed}", keep=[typed])
        self.assertNotIn(FAKE_TOKEN, reason)
        self.assertIn("[REDACTED]", reason)

    def test_the_kept_value_counts_toward_the_limit(self):
        typed = "C:\\" + "folder\\" * 40 + "file.txt"
        reason = winshell.sanitize_failure(f"missing {typed}", limit=60, keep=[typed])
        self.assertLessEqual(len(reason), 60)

    def test_ordinary_words_with_slashes_survive(self):
        self.assertEqual(winshell.sanitize_failure("use and/or wisely"), "use and/or wisely")


class TokenStoreLaunchTests(IsolatedCase):
    """token_store's PowerShell calls, with subprocess.run stubbed."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(release_log_then_remove, self.tmp)
        audit.setup(Path(self.tmp.name) / "a.log", "CRITICAL")

    def _capture(self, result):
        seen = {}

        def fake_run(args, **kwargs):
            seen["args"] = list(args)
            seen["env"] = kwargs.get("env")
            return result

        return seen, mock.patch.object(token_store.subprocess, "run", side_effect=fake_run)

    def assertLaunchedCleanly(self, seen):
        exe = seen["args"][0]
        self.assertTrue(exe == "powershell" or exe.lower().endswith("powershell.exe"), exe)
        self.assertIsNotNone(seen["env"], "PowerShell must get a child environment, not the parent's")
        self.assertNotIn("PSModulePath", seen["env"])
        self.assertNotIn("PSEXECUTIONPOLICYPREFERENCE", {k.upper() for k in seen["env"]})

    def test_acl_script_runs_without_the_parent_module_path(self):
        seen, patcher = self._capture(_proc(0, stdout=f'{{"owner": "{SID}", "entries": []}}'.encode()))
        with patcher, mock.patch.dict(os.environ, {"PSModulePath": PS7_MODULE_PATH,
                                                   "PSExecutionPolicyPreference": "Bypass"}):
            token_store._run_acl_script(token_store._ACL_READ_SCRIPT, Path(self.tmp.name), SID,
                                        directory=False)
        self.assertLaunchedCleanly(seen)
        self.assertEqual(seen["args"][1:3], ["-NoProfile", "-NonInteractive"])
        self.assertEqual(seen["args"][3], "-EncodedCommand")
        self.assertEqual(seen["env"]["TPA_ACL_SID"], SID)

    def test_sid_lookup_runs_without_the_parent_module_path(self):
        seen, patcher = self._capture(_proc(0, stdout=(SID + "\r\n").encode()))
        with patcher, mock.patch.dict(os.environ, {"PSModulePath": PS7_MODULE_PATH}):
            self.assertEqual(token_store._current_user_sid(), SID)
        self.assertLaunchedCleanly(seen)
        self.assertEqual(seen["args"][-2], "-Command")

    def test_a_full_windows_path_is_used_when_it_exists(self):
        with tempfile.TemporaryDirectory() as root:
            exe = Path(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"")
            seen, patcher = self._capture(_proc(0, stdout=SID.encode()))
            with patcher, mock.patch.dict(os.environ, {"SystemRoot": root}):
                token_store._current_user_sid()
        self.assertEqual(seen["args"][0], str(exe))

    def test_failure_keeps_a_short_reason_without_paths_or_the_token(self):
        audit.register_secret(FAKE_TOKEN)
        stderr = (CLIXML_ERROR + f" {FAKE_TOKEN}").encode("utf-8")
        _seen, patcher = self._capture(_proc(1, stderr=stderr))
        with patcher:
            self.assertIsNone(token_store._run_acl_script(token_store._ACL_READ_SCRIPT,
                                                          Path(self.tmp.name), SID, directory=False))
        reason = token_store.last_powershell_error()
        self.assertIn("could not be loaded", reason)
        self.assertNotIn("C:\\", reason)
        self.assertNotIn(FAKE_TOKEN, reason)
        self.assertLessEqual(len(reason), winshell.DEFAULT_REASON_LIMIT)

    def test_a_missing_powershell_is_reported_without_the_path(self):
        with mock.patch.object(token_store.subprocess, "run", side_effect=FileNotFoundError("C:\\x\\powershell.exe")):
            self.assertIsNone(token_store._current_user_sid())
        self.assertEqual(token_store.last_powershell_error(), "could not start PowerShell (FileNotFoundError)")

    def test_success_clears_the_previous_reason(self):
        _seen, patcher = self._capture(_proc(1, stderr=b"boom"))
        with patcher:
            token_store._current_user_sid()
        self.assertTrue(token_store.last_powershell_error())
        _seen, patcher = self._capture(_proc(0, stdout=SID.encode()))
        with patcher:
            token_store._current_user_sid()
        self.assertEqual(token_store.last_powershell_error(), "")


    def test_plain_token_save_refused_with_the_sanitized_reason(self):
        """The Round 9 user-visible failure: the save refuses, and the message says why (no token, no path)."""
        target = Path(self.tmp.name) / "store" / "token.json"
        failed = _proc(1, stderr=CLIXML_ERROR.encode("utf-8"))
        with mock.patch.object(token_store, "_on_windows", return_value=True), \
                mock.patch.object(token_store, "dpapi_available", return_value=False), \
                mock.patch.object(token_store, "_harden_dir", return_value=False), \
                mock.patch.object(token_store.subprocess, "run", return_value=failed):
            with self.assertRaises(RuntimeError) as caught:
                token_store.save_token(FAKE_TOKEN, path=target)
        message = str(caught.exception)
        self.assertIn("the token was not saved", message)
        self.assertIn("PowerShell said: Get-Acl", message)
        self.assertIn("could not be loaded", message)
        self.assertNotIn(FAKE_TOKEN.split(":", 1)[1], message)
        self.assertNotIn("runneradmin", message)
        self.assertNotIn("C:\\", message)
        self.assertFalse(target.exists(), "the plain-text token must not be left behind")


class ActionLaunchTests(IsolatedCase):
    """The actions' PowerShell (base.run_powershell) with Windows behaviour forced on."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(release_log_then_remove, self.tmp)
        audit.setup(Path(self.tmp.name) / "a.log", "CRITICAL")
        self.exe = str(Path(self.tmp.name) / "powershell.exe")

    def test_run_powershell_starts_the_full_path_without_the_module_path(self):
        seen = {}

        class FakeProcess:
            returncode = 0

            def __init__(self, argv, **kwargs):
                seen["argv"] = list(argv)
                seen["env"] = kwargs.get("env")  # None would mean the parent environment is inherited

            def communicate(self, timeout=None):
                return ("ok", "")

        with mock.patch.object(base, "IS_WINDOWS", True), \
                mock.patch.object(base.winshell, "windows_powershell_path", return_value=self.exe), \
                mock.patch.object(base.subprocess, "Popen", FakeProcess), \
                mock.patch.dict(os.environ, {"PSModulePath": PS7_MODULE_PATH}):
            result = base.run_powershell("Get-Date", 10)
        self.assertEqual(result.stdout, "ok")
        self.assertEqual(seen["argv"][0], self.exe)
        self.assertEqual(seen["argv"][1:4], ["-NoProfile", "-NonInteractive", "-Command"])
        self.assertIsNotNone(seen["env"], "run_powershell must pass a child environment")
        self.assertNotIn("PSModulePath", seen["env"])

    def test_no_powershell_at_all_is_an_action_error(self):
        with mock.patch.object(base, "IS_WINDOWS", True), \
                mock.patch.object(base.winshell, "windows_powershell_path", return_value="powershell"), \
                mock.patch.object(base.shutil, "which", return_value=None):
            with self.assertRaises(ActionError):
                base.powershell_exe()

    def test_clipboard_error_shows_a_sanitized_reason_not_raw_stderr(self):
        failed = _proc(1, stderr=CLIXML_ERROR.encode("utf-8"))
        with mock.patch.object(system, "IS_WINDOWS", True), \
                mock.patch.object(system, "run_powershell", return_value=failed):
            with self.assertRaises(ActionError) as caught:
                system._clipboard_write("hello")
        message = str(caught.exception)
        self.assertIn("could not be loaded", message)
        self.assertNotIn("C:\\", message)
        self.assertNotIn("runneradmin", message)
        self.assertNotIn("CLIXML", message)


class CoreActionOutputTests(IsolatedCase):
    """core.powershell shows the command's own output; core.open_target shows a sanitized failure."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(release_log_then_remove, self.tmp)
        audit.setup(Path(self.tmp.name) / "a.log", "CRITICAL")

    def _ctx(self):
        from assistant.actions.base import ActionContext
        config = SimpleNamespace(blocked_patterns=[], command_timeout=10,
                                 workspace_root=Path(self.tmp.name), max_output_chars=3000)
        return ActionContext(config=config)

    def test_command_stderr_loses_clixml_noise_but_keeps_the_error_text(self):
        from assistant.actions import core
        # run_process decodes to text, so the fake returns text as well
        proc = SimpleNamespace(returncode=1, stdout="partial output\n", stderr=CLIXML_ERROR)
        with mock.patch.object(core, "run_powershell", return_value=proc):
            result = core.powershell({"command": "Get-Acl x"}, self._ctx())
        self.assertFalse(result.ok)
        self.assertIn("partial output", result.output)
        self.assertIn("could not be loaded", result.output)
        self.assertNotIn("Preparing modules", result.output)
        self.assertNotIn("CLIXML", result.output)
        self.assertIn("exit code: 1", result.output)

    def test_open_fallback_failure_is_sanitized(self):
        from assistant.actions import core
        proc = SimpleNamespace(returncode=1, stdout="", stderr=CLIXML_ERROR + " C:\\Secret\\file.txt")
        target = Path(self.tmp.name) / "doc.txt"
        target.write_text("x", encoding="utf-8")
        with mock.patch.object(core, "IS_WINDOWS", True), \
                mock.patch.object(core, "_open_external", side_effect=OSError("no shell")), \
                mock.patch.object(core, "run_powershell", return_value=proc):
            with self.assertRaises(ActionError) as caught:
                core.open_target({"target": str(target)}, self._ctx())
        message = str(caught.exception)
        # The file the user asked for is shown as typed (the rule in winshell.sanitize_failure).
        prefix = f"Could not open '{target}': "
        self.assertTrue(message.startswith(prefix), message)
        # PowerShell's own text is cut to a short reason, and nothing from it leaks.
        reason = message[len(prefix):]
        self.assertTrue(reason, "a reason is required")
        self.assertLessEqual(len(reason), 120)
        self.assertNotIn("Secret", message)
        self.assertNotIn("runneradmin", message)
        self.assertNotIn("CLIXML", message)
        self.assertNotIn("Preparing modules", message)

    def test_open_fallback_starts_windows_powershell_through_the_clean_launcher(self):
        """Only Popen is stubbed here. run_powershell and the launcher run for real."""
        from assistant.actions import core
        seen: dict = {}
        exe = str(Path(self.tmp.name) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")

        class FakeProcess:
            returncode = 1

            def __init__(self, argv, **kwargs):
                seen["argv"] = list(argv)
                seen["env"] = kwargs.get("env")

            def communicate(self, timeout=None):
                return ("", CLIXML_ERROR)

        target = Path(self.tmp.name) / "doc.txt"
        target.write_text("x", encoding="utf-8")
        with mock.patch.object(core, "IS_WINDOWS", True), mock.patch.object(base, "IS_WINDOWS", True), \
                mock.patch.object(core, "_open_external", side_effect=OSError("no shell")), \
                mock.patch.object(base.winshell, "windows_powershell_path", return_value=exe), \
                mock.patch.object(base.subprocess, "Popen", FakeProcess), \
                mock.patch.dict(os.environ, {"PSModulePath": PS7_MODULE_PATH}):
            with self.assertRaises(ActionError):
                core.open_target({"target": str(target)}, self._ctx())
        self.assertEqual(seen["argv"][0], exe)
        self.assertEqual(seen["argv"][1:4], ["-NoProfile", "-NonInteractive", "-Command"])
        self.assertIn("Start-Process", seen["argv"][4])
        self.assertIsNotNone(seen["env"], "the fallback must pass a child environment")
        self.assertNotIn("PSModulePath", seen["env"])


class GuardSelfTests(IsolatedCase):
    """The guard itself: a real process or connection must fail the test, not run."""

    def expect_refused(self, call):
        """The refusal is what these tests check, so it is removed from the violation record."""
        before = len(VIOLATIONS)
        with self.assertRaises(ForbiddenCall):
            call()
        del VIOLATIONS[before:]

    def test_a_real_process_is_refused(self):
        self.expect_refused(lambda: subprocess.run(["tpa-not-a-real-program"], check=False))

    def test_a_real_connection_is_refused(self):
        # The guard refuses create_connection for every host before a socket exists, so no port is used.
        self.expect_refused(lambda: socket.create_connection(("127.0.0.1", 0)))

    def test_isolated_case_undoes_secrets_and_the_powershell_reason_after_each_test(self):
        probe = IsolatedCase("setUp")  # an instance that runs only the reset machinery
        probe.setUp()
        audit.register_secret("probe-secret-value-for-the-reset-test")
        token_store._last_powershell_error = "left behind by a test"
        base._ABORT.set()
        probe.doCleanups()
        self.assertNotIn("probe-secret-value-for-the-reset-test", audit.registered_secrets())
        self.assertEqual(token_store._last_powershell_error, "")
        self.assertFalse(base.abort_requested())

    def test_a_stub_inside_a_test_takes_precedence_and_then_the_guard_returns(self):
        with mock.patch.object(subprocess, "run", return_value="stub"):
            self.assertEqual(subprocess.run(["x"]), "stub")
        self.expect_refused(lambda: subprocess.run(["x"]))

if __name__ == "__main__":
    unittest.main()
