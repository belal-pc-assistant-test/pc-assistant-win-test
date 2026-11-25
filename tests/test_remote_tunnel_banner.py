"""Tunnel URL parsing and argv, download allowlist, cloudflared verification and pinning; banner protocol."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _bootstrap import PROJECT_DIR
from assistant.remote.banner import (RemoteBanner, RemoteError, banner_lines, format_remaining,
                                     parse_child_line, parse_parent_command)
from assistant.remote.tunnel import (PROVIDER_CLOUDFLARE, PROVIDER_LOCALHOST_RUN, TunnelError,
                                     _redirect_allowed, ensure_cloudflared, public_url_from_line,
                                     tunnel_argv, validate_download_url)

assert PROJECT_DIR.is_dir()


class TunnelUrlTests(unittest.TestCase):
    def test_cloudflare_address_is_extracted_from_log_lines(self):
        line = "2026-11-25T10:00:00Z INF |  https://quiet-otter-1234.trycloudflare.com  |"
        self.assertEqual(public_url_from_line(PROVIDER_CLOUDFLARE, line),
                         "https://quiet-otter-1234.trycloudflare.com")

    def test_localhost_run_address_is_extracted(self):
        self.assertEqual(public_url_from_line(PROVIDER_LOCALHOST_RUN,
                                              "Connect to https://abc123.lhr.life now"),
                         "https://abc123.lhr.life")
        self.assertEqual(public_url_from_line(PROVIDER_LOCALHOST_RUN,
                                              "https://abc123.localhost.run"),
                         "https://abc123.localhost.run")
        self.assertEqual(public_url_from_line(PROVIDER_LOCALHOST_RUN,
                                              "tunneled with tls termination, https://47970a.lhr.rocks"),
                         "https://47970a.lhr.rocks")

    def test_lookalike_hosts_and_plain_http_are_not_accepted(self):
        for line in ("https://evil.trycloudflare.com.attacker.example",
                     "https://x.trycloudflare.com.evil",
                     "http://quiet.trycloudflare.com",
                     "trycloudflare.com",
                     "https://-bad-.trycloudflare.com"):
            with self.subTest(line=line):
                self.assertIsNone(public_url_from_line(PROVIDER_CLOUDFLARE, line))

    def test_providers_do_not_cross_match(self):
        self.assertIsNone(public_url_from_line(PROVIDER_CLOUDFLARE, "https://a.lhr.life"))
        self.assertIsNone(public_url_from_line("unknown", "https://a.trycloudflare.com"))


class TunnelArgvTests(unittest.TestCase):
    def test_cloudflare_quick_tunnel_binds_the_loopback_port_only(self):
        argv = tunnel_argv(PROVIDER_CLOUDFLARE, 51234, cloudflared=Path("C:/tools/cloudflared.exe"), ssh=None)
        self.assertEqual(argv[1:], ["tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:51234"])

    def test_localhost_run_fallback_uses_ssh_reverse_forward(self):
        argv = tunnel_argv(PROVIDER_LOCALHOST_RUN, 51234, cloudflared=None, ssh="/usr/bin/ssh")
        self.assertIn("-R", argv)
        self.assertEqual(argv[argv.index("-R") + 1], "80:127.0.0.1:51234")
        self.assertEqual(argv[-1], "nokey@localhost.run")
        self.assertIn("ExitOnForwardFailure=yes", argv)

    def test_missing_program_means_no_argv(self):
        self.assertIsNone(tunnel_argv(PROVIDER_CLOUDFLARE, 1, cloudflared=None, ssh=None))
        self.assertIsNone(tunnel_argv(PROVIDER_LOCALHOST_RUN, 1, cloudflared=None, ssh=None))
        self.assertIsNone(tunnel_argv("other", 1, cloudflared=Path("x"), ssh="y"))


class DownloadAllowlistTests(unittest.TestCase):
    def test_only_the_official_cloudflared_release_path_is_accepted(self):
        validate_download_url("https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe")
        for url in ("http://github.com/cloudflare/cloudflared/releases/latest/x.exe",
                    "https://evil.example/cloudflared.exe",
                    "https://github.com/someone/fork/releases/x.exe",
                    "https://user:pw@github.com/cloudflare/cloudflared/releases/x.exe",
                    "https://github.com.evil.example/cloudflare/cloudflared/releases/x.exe"):
            with self.subTest(url=url):
                with self.assertRaises(TunnelError):
                    validate_download_url(url)

    def test_redirects_may_only_stay_on_github_asset_hosts(self):
        self.assertTrue(_redirect_allowed("https://objects.githubusercontent.com/github-production/x"))
        self.assertFalse(_redirect_allowed("https://evil.example/x"))
        self.assertFalse(_redirect_allowed("http://objects.githubusercontent.com/x"))


class CloudflaredInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tpa-tools-"))

    def fake_downloader(self, content: bytes, sha: str):
        def download(tools_dir: Path):
            partial = tools_dir / "cloudflared.exe.part"
            partial.write_bytes(content)
            return partial, sha
        return download

    def test_downloaded_binary_is_installed_only_after_verification(self):
        exe = ensure_cloudflared(self.tmp, downloader=self.fake_downloader(b"MZ", "a" * 64),
                                 verifier=lambda path: "cloudflared version 2026.1.0")
        self.assertEqual(exe, self.tmp / "cloudflared.exe")
        self.assertTrue(exe.is_file())
        self.assertFalse((self.tmp / "cloudflared.exe.part").exists())

    def test_pinned_hash_mismatch_is_refused_and_nothing_is_installed(self):
        with self.assertRaises(TunnelError):
            ensure_cloudflared(self.tmp, pinned_sha256="b" * 64,
                               downloader=self.fake_downloader(b"MZ", "a" * 64),
                               verifier=lambda path: "cloudflared")
        self.assertFalse((self.tmp / "cloudflared.exe").exists())
        self.assertFalse((self.tmp / "cloudflared.exe.part").exists())

    def test_failed_verification_removes_the_partial_file(self):
        def refuse(path):
            raise TunnelError("not cloudflared")
        with self.assertRaises(TunnelError):
            ensure_cloudflared(self.tmp, downloader=self.fake_downloader(b"junk", "a" * 64), verifier=refuse)
        self.assertFalse((self.tmp / "cloudflared.exe").exists())
        self.assertFalse((self.tmp / "cloudflared.exe.part").exists())

    def test_existing_binary_is_reused_when_it_verifies(self):
        (self.tmp / "cloudflared.exe").write_bytes(b"MZ")
        calls = []
        exe = ensure_cloudflared(self.tmp, downloader=lambda d: calls.append(d) or (d, ""),
                                 verifier=lambda path: "cloudflared")
        self.assertEqual(exe, self.tmp / "cloudflared.exe")
        self.assertEqual(calls, [], "no download when a verified copy exists")

    def test_corrupt_existing_binary_is_replaced(self):
        (self.tmp / "cloudflared.exe").write_bytes(b"broken")
        attempts = {"n": 0}

        def verifier(path):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise TunnelError("broken")
            return "cloudflared"

        ensure_cloudflared(self.tmp, downloader=self.fake_downloader(b"MZ", "a" * 64), verifier=verifier)
        self.assertTrue((self.tmp / "cloudflared.exe").is_file())


class BannerProtocolTests(unittest.TestCase):
    def test_time_is_formatted_for_humans(self):
        self.assertEqual(format_remaining(0), "00:00")
        self.assertEqual(format_remaining(65.9), "01:05")
        self.assertEqual(format_remaining(3725), "1:02:05")
        self.assertEqual(format_remaining(-4), "00:00")

    def test_banner_text_shows_time_viewer_state_and_the_hotkey(self):
        first, second = banner_lines(754, "connected")
        self.assertIn("12:34", first)
        self.assertIn("viewer connected", first)
        self.assertIn("Ctrl+Alt+Shift+Q", second)
        self.assertIn("waiting for viewer", banner_lines(10, "waiting")[0])

    def test_only_known_child_tokens_are_accepted(self):
        self.assertEqual(parse_child_line("HEARTBEAT\n"), "HEARTBEAT")
        self.assertEqual(parse_child_line("STOP button"), "STOP button")
        self.assertIsNone(parse_child_line("rm -rf /"))
        self.assertIsNone(parse_child_line("READY please"))

    def test_only_known_parent_commands_are_accepted(self):
        self.assertEqual(parse_parent_command("TIME 42"), ("TIME", "42"))
        self.assertEqual(parse_parent_command("VIEWER connected"), ("VIEWER", "connected"))
        self.assertEqual(parse_parent_command("END"), ("END", ""))
        for bad in ("TIME -5", "TIME abc", "VIEWER maybe", "EXEC calc", "END now", ""):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_parent_command(bad))


class FakeStdout:
    """Yields scripted lines, then blocks like a live pipe (never EOF while the child runs)."""

    def __init__(self, process: "FakeProcess", lines: list[bytes]) -> None:
        self._process = process
        self._lines = list(lines)

    async def readline(self) -> bytes:
        while not self._lines:
            if self._process.returncode is not None:
                return b""
            await asyncio.sleep(0.01)
        return self._lines.pop(0)


class FakeStdin:
    def __init__(self, process: "FakeProcess", obeys_end: bool) -> None:
        self._process = process
        self._obeys_end = obeys_end

    def write(self, data: bytes) -> None:
        if self._obeys_end and data.strip() == b"END":
            self._process.exit(0)

    def close(self) -> None:
        pass


class FakeProcess:
    """Behaves like the real banner child: exits on END (unless stubborn), and dies when killed."""

    def __init__(self, lines: list[bytes], obeys_end: bool = True) -> None:
        self.stdout = FakeStdout(self, lines)
        self.stdin = FakeStdin(self, obeys_end)
        self.returncode = None
        self.killed = False

    def exit(self, code: int) -> None:
        if self.returncode is None:
            self.returncode = code

    def kill(self) -> None:
        self.killed = True
        self.exit(-9)

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0.01)
        return self.returncode


class BannerOpenTests(unittest.IsolatedAsyncioTestCase):
    async def test_banner_that_never_reports_ready_means_nothing_starts(self):
        process = FakeProcess([b"something else\n"])

        async def fake_spawn(*argv, **kwargs):
            return process

        with mock.patch("assistant.sharing._spawn", fake_spawn):
            with self.assertRaises(RemoteError):
                await RemoteBanner.open(lambda reason: None, argv=["fake"], ready_timeout=1)
        self.assertIsNotNone(process.returncode, "the child must not be left running")
        self.assertFalse(process.killed, "a cooperative child exits on END without a kill")

    async def test_stubborn_banner_child_is_killed_after_the_grace_period(self):
        process = FakeProcess([b"something else\n"], obeys_end=False)

        async def fake_spawn(*argv, **kwargs):
            return process

        with mock.patch("assistant.sharing._spawn", fake_spawn):
            with self.assertRaises(RemoteError):
                await RemoteBanner.open(lambda reason: None, argv=["fake"], ready_timeout=0.5)
        self.assertTrue(process.killed)

    async def test_banner_child_runs_from_the_project_folder(self):
        process = FakeProcess([b"READY\n"])
        seen: dict = {}

        async def fake_spawn(*argv, **kwargs):
            seen.update(kwargs)
            return process

        with mock.patch("assistant.sharing._spawn", fake_spawn):
            banner = await RemoteBanner.open(lambda reason: None, ready_timeout=2)
        self.assertEqual(Path(seen["cwd"]).resolve(), PROJECT_DIR.resolve())
        await banner.close()

    async def test_banner_ready_then_reports_the_stop_button(self):
        process = FakeProcess([b"READY\n", b"STOP button\n"])
        reasons: list[str] = []

        async def fake_spawn(*argv, **kwargs):
            return process

        with mock.patch("assistant.sharing._spawn", fake_spawn):
            banner = await RemoteBanner.open(reasons.append, argv=["fake"], ready_timeout=2)
            for _ in range(50):
                if reasons:
                    break
                await asyncio.sleep(0.01)
        self.assertEqual(reasons, ["button"])
        await banner.close()


if __name__ == "__main__":
    unittest.main()
