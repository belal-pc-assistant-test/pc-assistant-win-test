"""Round 9 features: /health (read-only), /install and /uninstall through winget.

Everything here is hermetic: winget, PowerShell and subprocess are stubbed, and the
module guard fails any real process or network call. The real-Windows checks (winget
--version and a read-only health probe) are in test_windows_real.py.
"""
from __future__ import annotations

import asyncio
import configparser
import json
import os
import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _hermetic import IsolatedCase, ModuleGuard, release_log_then_remove  # noqa: E402
from _bootstrap import PROJECT_DIR  # noqa: E402
from assistant import app_allowlist, audit, health, pause, planner  # noqa: E402
from assistant.action_spec import BOT_ONLY_ACTIONS, RISK_HIGH, VALID_ACTIONS, risk_of  # noqa: E402
from assistant.actions import apps, dispatch  # noqa: E402
from assistant.actions.base import ActionContext, ActionError  # noqa: E402
from assistant.config import ConfigError, load_config  # noqa: E402

_guard = ModuleGuard()


def setUpModule():
    _guard.start()


def tearDownModule():
    _guard.stop()


EXPECTED_IDS = {
    "7zip": "7zip.7zip", "vlc": "VideoLAN.VLC", "notepadpp": "Notepad++.Notepad++",
    "git": "Git.Git", "python": "Python.Python.3.13", "chrome": "Google.Chrome",
    "firefox": "Mozilla.Firefox", "vscode": "Microsoft.VisualStudioCode", "zoom": "Zoom.Zoom",
    "whatsapp": "WhatsApp.WhatsApp", "telegram": "Telegram.TelegramDesktop",
}


def _cfg(jobs_dir: str, extra: dict | None = None):
    return SimpleNamespace(extra_allowed_apps=dict(extra or {}), jobs_file=Path(jobs_dir) / "jobs.json")


def _done(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class AllowlistTests(IsolatedCase):

    def test_builtin_list_has_exactly_the_agreed_apps_and_ids(self):
        builtin = app_allowlist.load_builtin()
        self.assertEqual({k: v["id"] for k, v in builtin.items()}, EXPECTED_IDS)

    def test_every_builtin_id_is_a_valid_winget_id(self):
        for name, entry in app_allowlist.load_builtin().items():
            self.assertRegex(entry["id"], app_allowlist.WINGET_ID_RE.pattern, name)
            self.assertNotIn("..", entry["id"])
            self.assertFalse(entry["id"].startswith("-"))

    def test_missing_or_damaged_builtin_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(app_allowlist.load_builtin(Path(tmp, "nope.json")), {})
            bad = Path(tmp, "bad.json")
            bad.write_text("{not json", encoding="utf-8")
            self.assertEqual(app_allowlist.load_builtin(bad), {})

    def test_bad_entries_in_the_builtin_file_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "list.json")
            path.write_text(json.dumps({"apps": {
                "good": {"id": "Vendor.Good", "title": "Good"},
                "badid": {"id": "Vendor..Bad", "title": "x"},
                "flag": {"id": "--force", "title": "x"},
                "Upper": {"id": "Vendor.Up", "title": "x"},
                "notdict": "Vendor.X",
            }}), encoding="utf-8")
            self.assertEqual(set(app_allowlist.load_builtin(path)), {"good"})

    def test_parse_extra_accepts_name_equals_id_pairs(self):
        self.assertEqual(app_allowlist.parse_extra("obsidian=Obsidian.Obsidian, tool=Vendor.Tool"),
                         {"obsidian": "Obsidian.Obsidian", "tool": "Vendor.Tool"})
        self.assertEqual(app_allowlist.parse_extra(""), {})

    def test_parse_extra_rejects_bad_values_with_a_plain_reason(self):
        for raw in ["x", "x=", "x=--force", "x=Vendor..Tool", "x=-Vendor", "bad name=V.T",
                    "x=Vendor.Tool, x=Vendor.Other"]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                app_allowlist.parse_extra(raw)

    def test_parse_extra_refuses_to_replace_a_builtin_name(self):
        with self.assertRaises(ValueError):
            app_allowlist.parse_extra("7zip=Evil.Tool", reserved=app_allowlist.load_builtin().keys())

    def test_config_reads_extra_apps_and_defaults_to_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = (PROJECT_DIR / "config.example.ini").read_text(encoding="utf-8")
            plain = Path(tmp, "plain.ini")
            plain.write_text(base, encoding="utf-8")
            self.assertEqual(load_config(plain).extra_allowed_apps, {})
            extra = Path(tmp, "extra.ini")
            extra.write_text(base.replace("extra_allowed_apps =", "extra_allowed_apps = obsidian=Obsidian.Obsidian"),
                             encoding="utf-8")
            self.assertEqual(load_config(extra).extra_allowed_apps, {"obsidian": "Obsidian.Obsidian"})
            broken = Path(tmp, "broken.ini")
            broken.write_text(base.replace("extra_allowed_apps =", "extra_allowed_apps = x=--force"),
                              encoding="utf-8")
            with self.assertRaises(ConfigError) as ctx:
                load_config(broken)
            self.assertIn("[apps] extra_allowed_apps", str(ctx.exception))

    def test_example_config_documents_the_option(self):
        parser = configparser.ConfigParser()
        parser.read(PROJECT_DIR / "config.example.ini", encoding="utf-8")
        self.assertIn("apps", parser)
        self.assertIn("extra_allowed_apps", parser["apps"])


class WingetArgumentTests(IsolatedCase):

    def test_install_argument_list_is_exact(self):
        self.assertEqual(apps.winget_args("app_install", "7zip.7zip"), [
            "install", "--exact", "--id", "7zip.7zip", "--source", "winget",
            "--accept-package-agreements", "--accept-source-agreements"])

    def test_uninstall_uses_only_the_flags_winget_uninstall_accepts(self):
        argv = apps.winget_args("app_uninstall", "7zip.7zip")
        self.assertEqual(argv, ["uninstall", "--exact", "--id", "7zip.7zip", "--source", "winget",
                                "--accept-source-agreements"])

    def test_no_forbidden_switch_can_appear_for_any_allowed_app(self):
        # Exact flag names (not substrings, so --id does not match -i).
        forbidden_flags = {"--force", "--ignore-security-hash", "--override", "--custom", "--manifest",
                           "-m", "--location", "--scope", "--silent", "-h", "--interactive", "-i",
                           "--header", "--installer-type", "--architecture", "--version", "-v"}
        forbidden_text = ("http", "://", ".exe", ".msi", ".msix", "\\", "/")
        for action in ("app_install", "app_uninstall"):
            for app_id in EXPECTED_IDS.values():
                for token in apps.winget_args(action, app_id):
                    self.assertNotIn(token.lower(), forbidden_flags, (action, token))
                    for bad in forbidden_text:
                        if bad == "/" and token == app_id:
                            continue  # ids never contain slashes anyway
                        self.assertNotIn(bad, token.lower(), (action, token))

    def test_unknown_action_is_refused_by_the_builder(self):
        with self.assertRaises(ValueError):
            apps.winget_args("app_repair", "7zip.7zip")


class WingetRunTests(IsolatedCase):

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = _cfg(self._tmp.name)
        self.ctx = ActionContext(config=self.cfg)
        self.calls: list = []

    def tearDown(self):
        self._tmp.cleanup()
        super().tearDown()

    def _run(self, action, params, returncode=0, stdout="", stderr="", exe="C:\\Tools\\winget.exe"):
        def fake_run(argv, timeout, cwd=None, env=None):
            self.calls.append({"argv": list(argv), "timeout": timeout, "env": env})
            return _done(returncode, stdout, stderr)
        with mock.patch.object(apps, "winget_path", return_value=exe), \
                mock.patch.object(apps, "run_process", side_effect=fake_run):
            return apps.app_install(params, self.ctx) if action == "app_install" \
                else apps.app_uninstall(params, self.ctx)

    def test_success_is_reported_only_for_exit_code_zero(self):
        result = self._run("app_install", {"name": "7zip", "id": "7zip.7zip"}, returncode=0)
        self.assertTrue(result.ok)
        self.assertIn("reported success (exit code 0)", result.output)
        self.assertEqual(self.calls[0]["argv"], ["C:\\Tools\\winget.exe", "install", "--exact", "--id",
                                                 "7zip.7zip", "--source", "winget",
                                                 "--accept-package-agreements", "--accept-source-agreements"])
        self.assertIsNotNone(self.calls[0]["env"])
        self.assertNotIn("PSMODULEPATH", {k.upper() for k in self.calls[0]["env"]})

    def test_nonzero_exit_is_a_failure_with_its_code_shown(self):
        result = self._run("app_install", {"name": "vlc", "id": "VideoLAN.VLC"}, returncode=1603,
                           stdout="Installer failed with exit code 1603\n")
        self.assertFalse(result.ok)
        self.assertIn("Exit code 1603 (0x00000643)", result.output)
        self.assertNotIn("success", result.output.lower())

    def test_uninstall_runs_the_uninstall_argument_list(self):
        result = self._run("app_uninstall", {"name": "git", "id": "Git.Git"}, returncode=0)
        self.assertTrue(result.ok)
        self.assertEqual(self.calls[0]["argv"][1:], ["uninstall", "--exact", "--id", "Git.Git",
                                                     "--source", "winget", "--accept-source-agreements"])
        self.assertIn("was removed", result.output)

    def test_missing_winget_says_so_and_runs_nothing(self):
        result = self._run("app_install", {"name": "vlc", "id": "VideoLAN.VLC"}, exe=None)
        self.assertFalse(result.ok)
        self.assertIn("winget is not available", result.output)
        self.assertEqual(self.calls, [])

    def test_paused_bot_installs_nothing(self):
        pause.save_pause(pause.pause_path(self.cfg.jobs_file), True, by=1)
        result = self._run("app_install", {"name": "vlc", "id": "VideoLAN.VLC"})
        self.assertFalse(result.ok)
        self.assertIn("Paused", result.output)
        self.assertEqual(self.calls, [])

    def test_damaged_pause_file_also_blocks(self):
        Path(self.cfg.jobs_file).parent.mkdir(parents=True, exist_ok=True)
        pause.pause_path(self.cfg.jobs_file).write_text("{broken", encoding="utf-8")
        result = self._run("app_install", {"name": "vlc", "id": "VideoLAN.VLC"})
        self.assertFalse(result.ok)
        self.assertEqual(self.calls, [])

    def test_name_not_on_the_list_runs_nothing(self):
        result = self._run("app_install", {"name": "evil", "id": "Evil.Tool"})
        self.assertFalse(result.ok)
        self.assertEqual(self.calls, [])

    def test_changed_id_for_an_allowed_name_runs_nothing(self):
        result = self._run("app_install", {"name": "vlc", "id": "Evil.Tool"})
        self.assertFalse(result.ok)
        self.assertIn("not on the allow-list", result.output)
        self.assertEqual(self.calls, [])

    def test_extra_app_from_config_is_allowed_only_when_configured(self):
        self.ctx = ActionContext(config=_cfg(self._tmp.name, {"obsidian": "Obsidian.Obsidian"}))
        ok = self._run("app_install", {"name": "obsidian", "id": "Obsidian.Obsidian"})
        self.assertTrue(ok.ok)
        self.ctx = ActionContext(config=_cfg(self._tmp.name))
        refused = self._run("app_install", {"name": "obsidian", "id": "Obsidian.Obsidian"})
        self.assertFalse(refused.ok)

    def test_malformed_params_run_nothing(self):
        for params in ({"name": "vlc"}, {"name": "vlc", "id": "VideoLAN.VLC", "force": "1"}, {}):
            with self.subTest(params=params):
                self.assertFalse(self._run("app_install", params).ok)
        self.assertEqual(self.calls, [])

    def test_failure_text_is_sanitized(self):
        raw = ("Downloading 45%\n██████ 10.0 MB / 20.0 MB\n"
               "Error at C:\\Users\\alice\\Downloads\\setup.exe while using 123456789:AAHsecretTokenValueForRedaction_abcdef\n"
               "Installer exited /home/alice/private/file.txt\n")
        result = self._run("app_install", {"name": "vlc", "id": "VideoLAN.VLC"}, returncode=2, stdout=raw)
        self.assertNotIn("alice", result.output)
        self.assertNotIn("C:\\", result.output)
        self.assertNotIn("123456789:AAH", result.output)
        self.assertNotIn("██", result.output)
        self.assertNotIn("10.0 MB / 20.0 MB", result.output)

    def test_reason_shows_only_the_last_meaningful_lines(self):
        text = "\n".join(f"line {i}" for i in range(10)) + "\n\n  45%\n"
        reason = apps.failure_reason(text)
        self.assertIn("line 9", reason)
        self.assertNotIn("line 0", reason)

    def test_timeout_surfaces_as_an_action_error_not_success(self):
        def timeout(argv, timeout, cwd=None, env=None):
            raise ActionError("Command timed out after 1800s and was killed.")
        with mock.patch.object(apps, "winget_path", return_value="winget"), \
                mock.patch.object(apps, "run_process", side_effect=timeout):
            result = dispatch.execute_step(
                __import__("assistant.planner", fromlist=["Step"]).Step("app_install", {"name": "vlc", "id": "VideoLAN.VLC"}),
                self.cfg)
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.output)


class PlannerCannotEmitAppStepsTests(IsolatedCase):

    def test_app_actions_are_bot_only_and_high_risk(self):
        for action in ("app_install", "app_uninstall"):
            self.assertIn(action, BOT_ONLY_ACTIONS)
            self.assertNotIn(action, VALID_ACTIONS)
            self.assertEqual(risk_of(action), RISK_HIGH)

    def test_planner_schema_and_parser_drop_them(self):
        self.assertNotIn("app_install", json.dumps(planner.plan_json_schema()))
        raw = json.dumps({"summary": "x", "steps": [
            {"action": "app_install", "params": {"name": "vlc", "id": "VideoLAN.VLC"}},
            {"action": "reply", "params": {"text": "hi"}}]})
        plan = planner.build_plan(raw, "direct", "", 6, workspace_root=None)
        self.assertEqual([step.action for step in plan.steps], ["reply"])

    def test_health_is_not_a_plannable_action(self):
        self.assertNotIn("health", VALID_ACTIONS)
        self.assertNotIn("health", BOT_ONLY_ACTIONS)

    def test_dispatch_knows_the_bot_only_actions(self):
        self.assertIn("app_install", dispatch.HANDLERS)
        self.assertIn("app_uninstall", dispatch.HANDLERS)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

GOOD_OS = {"totalKB": 16 * 1024 * 1024, "freeKB": 8 * 1024 * 1024,
           "bootUnix": int(datetime(2026, 11, 24, 8, 0).timestamp())}


def _healthy_ps(script: str, what: str):
    if "Win32_OperatingSystem" in script:
        return dict(GOOD_OS)
    if "RebootRequired" in script:
        return {"pending": False}
    if "MpComputerStatus" in script:
        return {"rtp": True, "sigAgeDays": 1}
    if "NetFirewallProfile" in script:
        return [{"name": "Domain", "enabled": True}, {"name": "Private", "enabled": True},
                {"name": "Public", "enabled": True}]
    if "wuauserv" in script:
        return {"status": "Running", "startType": "Automatic"}
    raise AssertionError("unexpected probe")


def _usage(free_gb, total_gb):
    return SimpleNamespace(free=int(free_gb * health.GIB), total=int(total_gb * health.GIB))


NOW = datetime(2026, 11, 25, 12, 0)


def _run(ps=_healthy_ps, drives=("C",), usage=None):
    usage = usage or (lambda path: _usage(100, 200))
    return health.run_health(windows=True, powershell=ps, drives=lambda: list(drives),
                             disk_usage=usage, now=NOW)


def _by_title(checks):
    return {c.title: c for c in checks}


class HealthTests(IsolatedCase):

    def test_healthy_pc_is_all_ok(self):
        checks = _by_title(_run())
        self.assertEqual(checks["Disk C"].status, health.OK)
        self.assertEqual(checks["Memory"].status, health.OK)
        self.assertEqual(checks["Restart pending"].status, health.OK)
        self.assertEqual(checks["Windows Defender"].status, health.OK)
        self.assertEqual(checks["Firewall"].status, health.OK)
        self.assertEqual(checks["Windows Update service"].status, health.OK)

    def test_warnings_fire_for_each_real_problem(self):
        def ps(script, what):
            if "Win32_OperatingSystem" in script:
                return {"totalKB": 16 * 1024 * 1024, "freeKB": 512 * 1024,
                        "bootUnix": int(datetime(2026, 10, 1, 8, 0).timestamp())}
            if "RebootRequired" in script:
                return {"pending": True}
            if "MpComputerStatus" in script:
                return {"rtp": False, "sigAgeDays": 9}
            if "NetFirewallProfile" in script:
                return [{"name": "Domain", "enabled": True}, {"name": "Public", "enabled": False}]
            return {"status": "Stopped", "startType": "Disabled"}
        checks = _by_title(_run(ps=ps, drives=("C", "D"),
                                usage=lambda p: _usage(5, 500) if p[0] == "D" else _usage(100, 200)))
        for title in ("Disk D", "Memory", "Uptime", "Restart pending", "Windows Defender",
                      "Firewall", "Windows Update service"):
            self.assertEqual(checks[title].status, health.WARN, title)
            self.assertTrue(checks[title].advice, title)
        self.assertEqual(checks["Disk C"].status, health.OK)
        self.assertEqual(checks["Last boot"].status, health.OK)

    def test_disk_threshold_is_ten_percent_free(self):
        self.assertEqual(_by_title(_run(usage=lambda p: _usage(20, 200)))["Disk C"].status, health.OK)
        self.assertEqual(_by_title(_run(usage=lambda p: _usage(19, 200)))["Disk C"].status, health.WARN)

    def test_each_probe_failure_is_unknown_with_a_reason_and_never_raises(self):
        failures = {
            "raises ActionError": ActionError("PowerShell said something"),
            "raises OSError": OSError("no such file"),
            "returns bad json": None,
            "wrong shape": "shape",
        }
        for label, failure in failures.items():
            def ps(script, what, failure=failure):
                if failure is None:
                    raise _Bad()
                if failure == "shape":
                    return {"unexpected": 1}
                raise failure
            with self.subTest(case=label):
                checks = _run(ps=ps)
                self.assertEqual(len(checks), 8)  # one drive + 7 items
                for check in checks:
                    if check.title in ("Disk C",):
                        continue
                    self.assertEqual(check.status, health.UNKNOWN, check.title)
                    self.assertTrue(check.detail.startswith("unknown:"), check.detail)

    def test_drive_enumeration_failure_is_one_unknown_line(self):
        def boom():
            raise OSError("no drive list")
        checks = health.run_health(windows=True, powershell=_healthy_ps, drives=boom,
                                   disk_usage=lambda p: _usage(1, 2), now=NOW)
        disk = [c for c in checks if c.title.startswith("Disk")]
        self.assertEqual(len(disk), 1)
        self.assertEqual(disk[0].status, health.UNKNOWN)

    def test_drive_usage_failure_is_unknown_for_that_drive_only(self):
        def usage(path):
            if path.startswith("D"):
                raise OSError("device not ready")
            return _usage(100, 200)
        checks = _by_title(_run(drives=("C", "D"), usage=usage))
        self.assertEqual(checks["Disk C"].status, health.OK)
        self.assertEqual(checks["Disk D"].status, health.UNKNOWN)

    def test_off_windows_every_item_is_unknown_and_nothing_is_probed(self):
        def ps(script, what):
            raise AssertionError("must not probe off Windows")
        checks = health.run_health(windows=False, powershell=ps)
        self.assertEqual(len(checks), 8)
        self.assertTrue(all(c.status == health.UNKNOWN for c in checks))
        self.assertTrue(all("Windows only" in c.detail for c in checks))

    def test_every_probe_goes_through_the_shared_launcher(self):
        seen = []

        def fake_launcher(script, timeout, cwd=None):
            seen.append(script)
            return _done(0, json.dumps({"pending": False}) if "RebootRequired" in script else "{}")
        with mock.patch.object(health, "run_powershell", side_effect=fake_launcher):
            checks = health.run_health(windows=True, drives=lambda: [], disk_usage=None, now=NOW)
        self.assertEqual(len(seen), 6)  # memory, boot time, reboot, defender, firewall, update
        self.assertTrue(checks)

    def test_probe_scripts_are_read_only(self):
        scripts = [health._OS_SCRIPT, health._REBOOT_SCRIPT, health._DEFENDER_SCRIPT,
                   health._FIREWALL_SCRIPT, health._UPDATE_SCRIPT]
        changing = re.compile(r"\b(Set|Start|Stop|Restart|Remove|New|Invoke|Install|Uninstall|Enable|"
                              r"Disable|Add|Clear|Write|Update|Register|Unregister|Reset|Copy|Move)-\w+", re.I)
        for script in scripts:
            self.assertIsNone(changing.search(script), script[:80])
            self.assertNotIn("-Force", script)

    def test_output_has_no_user_name_path_or_serial(self):
        text = health.render(_run(drives=("C", "D")), "en")
        user = os.environ.get("USERNAME") or os.environ.get("USER") or "tpa-user"
        self.assertNotIn(user, text)
        self.assertIsNone(re.search(r"[A-Za-z]:\\", text))
        self.assertIsNone(re.search(r"/home/|/Users/|\\Users\\", text))
        self.assertIsNone(re.search(r"[Ss]erial", text))

    def test_render_has_markers_and_one_advice_line_per_warning(self):
        def ps(script, what):
            if "MpComputerStatus" in script:
                return {"rtp": False, "sigAgeDays": 1}
            return _healthy_ps(script, what)
        text = health.render(_run(ps=ps), "en")
        self.assertIn("✅ OK ·", text)
        self.assertIn("⚠️ WARNING · Windows Defender", text)
        self.assertEqual(text.count("↳"), 1)
        self.assertIn("1 warning(s), 0 unknown.", text)

    def test_arabic_verdict_words_only_for_an_arabic_user(self):
        checks = _run()
        self.assertIn("✅ سليم", health.render(checks, "ar"))
        self.assertIn("✅ OK", health.render(checks, "en"))
        self.assertNotIn("سليم", health.render(checks, "en"))

    def test_html_is_escaped_in_detail_text(self):
        check = health.Check("Test", health.WARN, "<b>x</b> & y", "<i>fix</i> & go")
        text = health.render([check], "en")
        self.assertNotIn("<b>x</b>", text)
        self.assertIn("&lt;b&gt;x&lt;/b&gt; &amp; y", text)

    def test_default_ps_path_uses_shared_launcher_and_parses_last_json_line(self):
        out = "some noise\n" + json.dumps({"pending": True}) + "\n"
        with mock.patch.object(health, "run_powershell", return_value=_done(0, out, "")):
            self.assertEqual(health._ps_json(health._REBOOT_SCRIPT, "restart status"), {"pending": True})

    def test_default_ps_path_turns_a_failure_into_a_short_reason(self):
        clixml = ("#< CLIXML\n<Objs><S S=\"Error\">Get-MpComputerStatus : not recognised at "
                  "C:\\Users\\alice\\x.ps1:1 char:1</S></Objs>")
        with mock.patch.object(health, "run_powershell", return_value=_done(1, "", clixml)):
            with self.assertRaises(health._Unknown) as ctx:
                health._ps_json(health._DEFENDER_SCRIPT, "Windows Defender status")
        reason = str(ctx.exception)
        self.assertNotIn("alice", reason)
        self.assertNotIn("C:\\", reason)
        self.assertIn("Windows Defender status could not be read", reason)

    def test_boot_script_returns_unix_seconds_not_a_formatted_date(self):
        self.assertIn("ToUnixTimeSeconds", health._OS_SCRIPT)
        self.assertNotIn("ToString", health._OS_SCRIPT)

    def test_real_drive_letter_helper_is_windows_only(self):
        if os.name == "nt":
            self.skipTest("the Linux refusal is only checked off Windows")
        with self.assertRaises(health._Unknown):
            health._fixed_drive_letters()


class _Bad(Exception):
    pass


class HealthWiringTests(IsolatedCase):

    def test_help_and_command_list_name_the_new_commands(self):
        from assistant import bot
        self.assertIn("/health", bot.MSG["help"])
        self.assertIn("/install NAME", bot.MSG["help"])
        self.assertIn("/uninstall NAME", bot.MSG["help"])
        self.assertLess(len(bot.MSG["help"]), 4096)
        source = (PROJECT_DIR / "assistant" / "bot.py").read_text(encoding="utf-8")
        for command in ("health", "install", "uninstall"):
            self.assertIn(f'BotCommand("{command}"', source)
            self.assertIn(f'CommandHandler("{command}"', source)

    def test_health_is_not_exempt_from_pause_and_needs_no_approval(self):
        from assistant.pause import is_exempt
        self.assertFalse(is_exempt("/health"))
        self.assertFalse(is_exempt("/install vlc"))
        self.assertTrue(is_exempt("/status"))

    def test_macro_save_refuses_app_steps(self):
        source = (PROJECT_DIR / "assistant" / "bot.py").read_text(encoding="utf-8")
        self.assertIn("'app_install', 'app_uninstall'", source)


class AppCommandFlowTests(IsolatedCase):
    """The real /install, /uninstall and /health handlers, with fake Telegram objects.

    /health runs as if on Windows, but every probe is stubbed: no PowerShell, no drive
    query, no disk query. No real process starts on any platform.
    """

    def setUp(self):
        super().setUp()
        from test_flow import make_config, make_context
        self._tmp = tempfile.TemporaryDirectory()
        # One cleanup closes the log and then removes the folder (Windows keeps it locked).
        self.addCleanup(release_log_then_remove, self._tmp)
        self.dir = Path(self._tmp.name)
        self.config = make_config(self.dir)
        audit.setup(self.dir / "logs" / "a.log", "CRITICAL")
        self.context = make_context(self.config)
        self.probe_calls: list[str] = []
        self._stub_health_probes()

    def _patch(self, patcher):
        patcher.start()
        self.addCleanup(patcher.stop)

    def _stub_health_probes(self):
        real_run_health = health.run_health

        def windows_health(**kwargs):
            return real_run_health(**{"windows": True, **kwargs})

        def stub_probe(script, what):
            self.probe_calls.append(what)
            return _healthy_ps(script, what)

        def no_powershell(script, timeout, cwd=None):
            raise AssertionError("/health must use the stubbed probe, never PowerShell")

        self._patch(mock.patch.object(health, "run_health", windows_health))
        self._patch(mock.patch.object(health, "_ps_json", stub_probe))
        self._patch(mock.patch.object(health, "run_powershell", no_powershell))
        self._patch(mock.patch.object(health, "_fixed_drive_letters", lambda: ["C"]))
        self._patch(mock.patch("shutil.disk_usage", lambda path: _usage(100, 200)))

    def _send(self, handler, text, args, user_id=None, winget="C:\\Tools\\winget.exe"):
        from test_flow import FakeMessage, OWNER_ID, make_update
        from assistant import bot as botmod
        message = FakeMessage(text)
        self.context.args = args
        update = make_update(message=message, user_id=OWNER_ID if user_id is None else user_id)
        with mock.patch.object(botmod, "winget_path", return_value=winget), \
                mock.patch.object(apps, "run_process", side_effect=AssertionError("no process may run")):
            asyncio.run(getattr(botmod, handler)(update, self.context))
        return message

    def _plans(self, action=None):
        plans = list(self.context.application.bot_data["plans"].values())
        if action is None:
            return plans
        return [p for p in plans if any(s.action == action for s in p.plan.steps)]

    def test_install_creates_a_high_risk_plan_with_the_exact_id(self):
        message = self._send("cmd_install", "/install vlc", ["vlc"])
        plans = self._plans("app_install")
        self.assertEqual(len(plans), 1, message.replies)
        step = plans[0].plan.steps[0]
        self.assertEqual(step.params, {"name": "vlc", "id": "VideoLAN.VLC"})
        self.assertIn("VideoLAN.VLC", plans[0].plan.summary)
        self.assertIn("source winget only", plans[0].plan.summary)
        self.assertIn("latest version", plans[0].plan.summary)
        self.assertIn("UAC", plans[0].plan.summary)
        self.assertEqual(plans[0].plan.risk, RISK_HIGH)

    def test_uninstall_creates_a_plan_for_the_listed_id(self):
        self._send("cmd_uninstall", "/uninstall git", ["git"])
        plans = self._plans("app_uninstall")
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].plan.steps[0].params, {"name": "git", "id": "Git.Git"})

    def test_name_is_case_insensitive_but_must_be_listed(self):
        self._send("cmd_install", "/install VLC", ["VLC"])
        self.assertEqual(len(self._plans("app_install")), 1)

    def test_unlisted_name_is_refused_and_names_the_allow_list(self):
        message = self._send("cmd_install", "/install evil", ["evil"])
        self.assertEqual(self._plans(), [])
        text = message.replies[-1]
        self.assertIn("allow-list", text)
        self.assertIn("extra_allowed_apps", text)
        self.assertIn("7zip", text)

    def test_missing_winget_is_said_and_nothing_is_proposed(self):
        message = self._send("cmd_install", "/install vlc", ["vlc"], winget=None)
        self.assertEqual(self._plans(), [])
        self.assertIn("winget is not available", message.replies[-1])

    def test_usage_is_shown_without_a_name(self):
        message = self._send("cmd_install", "/install", [])
        self.assertIn("Usage: /install NAME", message.replies[-1])
        self.assertEqual(self._plans(), [])

    def test_a_configured_extra_app_can_be_requested(self):
        self.config.extra_allowed_apps = {"obsidian": "Obsidian.Obsidian"}
        self._send("cmd_install", "/install obsidian", ["obsidian"])
        plans = self._plans("app_install")
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].plan.steps[0].params["id"], "Obsidian.Obsidian")

    def test_pause_gate_stops_an_install_request(self):
        from telegram.ext import ApplicationHandlerStop
        from test_flow import FakeMessage, OWNER_ID, make_update
        from assistant import bot as botmod
        self.context.application.bot_data["paused"] = True
        message = FakeMessage("/install vlc")
        update = make_update(message=message, user_id=OWNER_ID)
        with self.assertRaises(ApplicationHandlerStop):
            asyncio.run(botmod.pause_gate(update, self.context))
        self.assertEqual(self._plans(), [])

    def test_health_replies_without_creating_a_plan(self):
        message = self._send("cmd_health", "/health", [])
        self.assertTrue(any("PC health check" in text for text in message.replies))
        self.assertEqual(self._plans(), [])
        self.assertTrue(self.probe_calls, "the stubbed health probes were not used")

    def test_health_refuses_other_users(self):
        from test_flow import OTHER_ID
        message = self._send("cmd_health", "/health", [], user_id=OTHER_ID)
        self.assertFalse(any("PC health check" in text for text in message.replies))

    def test_health_is_blocked_while_paused(self):
        from telegram.ext import ApplicationHandlerStop
        from test_flow import FakeMessage, OWNER_ID, make_update
        from assistant import bot as botmod
        self.context.application.bot_data["paused"] = True
        with self.assertRaises(ApplicationHandlerStop):
            asyncio.run(botmod.pause_gate(make_update(message=FakeMessage("/health"), user_id=OWNER_ID),
                                          self.context))


class LogHandleReleaseTests(IsolatedCase):
    """The log file must be closed before its folder is deleted (Windows WinError 32)."""

    def setUp(self):
        super().setUp()
        audit.close()  # start clean, even if another test configured the logger
        self.addCleanup(audit.close)

    def test_release_helper_closes_the_file_then_removes_the_folder(self):
        from logging.handlers import RotatingFileHandler
        tmp = tempfile.TemporaryDirectory()
        log = Path(tmp.name, "logs", "a.log")
        audit.setup(log, "CRITICAL")
        audit.event("probe")
        handlers = [h for h in audit._LOGGER.handlers if isinstance(h, RotatingFileHandler)]
        self.assertEqual(len(handlers), 1)
        self.assertTrue(log.exists())
        release_log_then_remove(tmp)
        self.assertIsNone(handlers[0].stream, "the log file is still open")
        self.assertEqual(audit._LOGGER.handlers, [])
        self.assertFalse(Path(tmp.name).exists())

    def test_bot_shutdown_releases_the_log_file(self):
        from logging.handlers import RotatingFileHandler
        from assistant import bot as botmod
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(release_log_then_remove, tmp)
        audit.setup(Path(tmp.name, "logs", "a.log"), "CRITICAL")
        handler = next(h for h in audit._LOGGER.handlers if isinstance(h, RotatingFileHandler))
        asyncio.run(botmod._post_shutdown(SimpleNamespace(bot_data={})))
        self.assertIsNone(handler.stream, "bot shutdown left the log file open")
        self.assertEqual(audit._LOGGER.handlers, [])


class ReadmeRound9Tests(IsolatedCase):

    def setUp(self):
        super().setUp()
        self.readme = (PROJECT_DIR / "README.md").read_text(encoding="utf-8")

    def test_readme_documents_the_new_commands_and_rules(self):
        for needle in ("/health", "/install", "/uninstall", "extra_allowed_apps", "app_allowlist.json",
                       "winget", "UAC"):
            self.assertIn(needle, self.readme, needle)

    def test_arabic_quick_start_still_exists(self):
        self.assertIn("## البدء السريع", self.readme)


if __name__ == "__main__":
    unittest.main()
