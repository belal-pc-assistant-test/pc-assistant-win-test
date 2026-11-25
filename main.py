"""Telegram PC Assistant — entry point.

Usage on Windows (see README.md):

    python main.py                 # start the bot (asks for the token on first run)
    python main.py --set-token     # save/replace the bot token (hidden input)
    python main.py --forget-token  # delete the stored token
    python main.py --check         # self test: config, token, planners, PowerShell
    python main.py --plan "text"   # dry run: show the plan for a request, run nothing
    python main.py --config other.ini
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import platform
import shutil
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from assistant import audit, elevation, token_store                # noqa: E402
from assistant.config import Config, ConfigError, load_config      # noqa: E402
from assistant.action_spec import VALID_ACTIONS                      # noqa: E402
from assistant.planner import (                                    # noqa: E402
    HAVE_JSONSCHEMA,
    Planner,
    rule_based_plan,
)

__version__ = "3.1.0"


# ---------------------------------------------------------------------------
# Platform helpers
# ---------------------------------------------------------------------------

def ensure_utf8_console() -> None:
    """Make Arabic output work in the Windows console / redirected logs."""
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)  # type: ignore[attr-defined]
            ctypes.windll.kernel32.SetConsoleCP(65001)        # type: ignore[attr-defined]
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def enable_dpi_awareness() -> None:
    """Make screen capture pixel-accurate on scaled displays (Windows only)."""
    if os.name != "nt":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)      # type: ignore[attr-defined]
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()           # type: ignore[attr-defined]
    except Exception:
        pass


def build_planner(config: Config) -> Planner:
    return Planner.from_config(config)


def redact(text: str, config: Config | None = None) -> str:
    """Never let the bot token appear in the console or the log."""
    if config and config.bot_token:
        text = text.replace(config.bot_token, "[REDACTED]")
    return text


# ---------------------------------------------------------------------------
# Token handling
# ---------------------------------------------------------------------------

def ensure_token(config: Config, allow_prompt: bool = True) -> bool:
    """Make sure we have a working token; asks for it on first run."""
    if config.bot_token:
        return True

    if not allow_prompt:
        return False

    token = token_store.ask_for_token_interactively()
    if not token:
        return False
    audit.register_secret(token)
    config.bot_token = token
    config.token_source = "store"
    return True


def cmd_set_token() -> int:
    print("\n=== Save the bot token ===")
    print(f"Current store: {token_store.token_path()}")
    print(f"Protection   : {token_store.protection_of()}")
    existing = token_store.load_stored_token()
    if existing:
        print("Stored token : present")
    print("(Get a token from @BotFather → /mybots → API Token)\n")

    token = token_store.ask_for_token_interactively()
    if not token:
        print("Nothing saved.")
        return 1
    print("\n✅ Done. Start the bot with:  python main.py")
    return 0


def cmd_forget_token() -> int:
    removed = token_store.delete_token()
    print(f"Stored token file: {token_store.token_path()}")
    print("✅ Deleted." if removed else "ℹ️ There was no stored token.")
    if (PROJECT_DIR / "token.txt").exists():
        print("⚠️ token.txt was not removed and has priority over other sources.")
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        print("⚠️ Note: TELEGRAM_BOT_TOKEN is still set in this environment.")
    return 0


# ---------------------------------------------------------------------------
# --plan : dry run (no Telegram, nothing executed)
# ---------------------------------------------------------------------------

def dry_run(config: Config, request: str) -> int:
    def safe_print(value=""):
        print(redact(str(value), config))

    # A CLI dry run must not silently send request data to a remote planner.
    from assistant.bot import needs_external_planner
    if needs_external_planner(config):
        planner = Planner(mode="keywords", workspace_root=config.workspace_root, max_steps=config.max_plan_steps)
        safe_print("External planner skipped for dry run; using offline keywords.")
    else:
        planner = build_planner(config)
    plan = planner.plan(request)
    safe_print(f"\nRequest : {request}")
    safe_print(f"Planner : {plan.planner}  ({plan.label()})")
    if plan.note:
        safe_print(f"Note    : {plan.note}")
    safe_print(f"Risk    : {plan.risk}")
    safe_print(f"Summary : {plan.summary}")
    safe_print("Steps   :")
    for index, step in enumerate(plan.steps, start=1):
        safe_print(f"  {index}. {step.action}")
        for key, value in step.params.items():
            rendered = redact(str(value), config)
            if len(rendered) > 300:
                rendered = rendered[:300] + "…"
            safe_print(f"       {key} = {rendered}")
        if step.warning:
            safe_print(f"       {step.warning}")
    safe_print("\n(DRY RUN — nothing was executed; the bot would ask for ✅ Yes first.)\n")
    if os.environ.get("ASSISTANT_PRINT_JSON") == "1":
        safe_print(json.dumps(
            {"summary": plan.summary,
             "steps": [{"action": s.action, "params": s.params} for s in plan.steps]},
            ensure_ascii=False, indent=2,
        ))
    return 0


# ---------------------------------------------------------------------------
# --check : environment self test
# ---------------------------------------------------------------------------

def _ok(label: str, detail: str = "") -> None:
    print(f"  [ OK ] {label}" + (f" — {detail}" if detail else ""))


def _warn(label: str, detail: str = "") -> None:
    print(f"  [WARN] {label}" + (f" — {detail}" if detail else ""))


def _fail(label: str, detail: str = "") -> None:
    print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))


def _notify_owners(config: Config, text: str) -> bool:
    """Best-effort Telegram notice to the allowed users. Never raises."""
    if not config.bot_token or not config.allowed_user_ids:
        return False
    import asyncio

    from telegram import Bot

    from assistant.outbound import RedactingHTTPXRequest

    async def send() -> None:
        bot = Bot(token=config.bot_token, request=RedactingHTTPXRequest(connection_pool_size=1))
        async with bot:
            for user_id in sorted(config.allowed_user_ids):
                await bot.send_message(chat_id=user_id, text=text)

    try:
        asyncio.run(asyncio.wait_for(send(), 20))
        return True
    except Exception as exc:  # the notice is optional; the refusal already happened
        print(redact(f"   Could not send the Telegram notice: {exc}", config), file=sys.stderr)
        return False


def _refuse_elevated(config_path: str | None) -> int:
    """Print the refusal, tell the owners on Telegram if possible, and exit with code 5."""
    print("\n" + elevation.REFUSAL_TEXT + "\n", file=sys.stderr)
    try:
        config = load_config(config_path)
    except ConfigError:
        return elevation.ELEVATED_EXIT_CODE
    audit.setup(config.log_file, config.log_level)
    audit.event("refused_elevated")
    _notify_owners(config, elevation.REFUSAL_TEXT)
    audit.close()
    return elevation.ELEVATED_EXIT_CODE


def self_check(config: Config) -> int:
    problems = 0
    print("\n=== Telegram PC Assistant self check ===")
    print(f"Version         : {__version__}")
    print(f"Python          : {sys.version.split()[0]} ({platform.system()} {platform.release()})")
    print(f"Project folder  : {PROJECT_DIR}")
    print(f"Config file     : {config.config_path}")

    # 0) Elevation: the assistant refuses to start as Administrator.
    print("\n[0] Running as")
    elevated = elevation.is_administrator()
    if elevated is True:
        _fail("administrator", "yes: the assistant refuses to start elevated; "
                               "start it from a normal (non-admin) window")
        problems += 1
    elif elevated is False:
        _ok("administrator", "no")
    else:
        _warn("administrator", "could not be determined on this system")

    # 1) Config sanity
    print("\n[1] Configuration")
    _ok("planner mode", f"{config.planner_mode}  (chain: {' → '.join(config.planner_chain())})")
    if config.planner_mode == "cloud":
        _warn("privacy", "cloud mode: request text is sent to pollinations.ai")
    _ok("allowed user ids (may ask)", str(sorted(config.allowed_user_ids)))
    _ok("approver ids (may confirm)", str(sorted(config.approver_user_ids))
        + ("  ⚠️ two-person rule" if config.two_person_rule else ""))
    _ok("working folder", str(config.workspace_root))
    _ok("step cap / timeout", f"{config.max_plan_steps} steps, {config.command_timeout}s")
    _ok("actions available", f"{len(VALID_ACTIONS)} (transfers ≤ {config.max_send_file_mb} MB)")

    # 2) Token
    print("\n[2] Bot token")
    if not config.bot_token:
        _warn("no token yet", "run: python main.py --set-token  (or set TELEGRAM_BOT_TOKEN)")
    else:
        _ok("token present", "yes")
        _ok("token source", config.token_source or "unknown")
        _ok("store path", str(config.token_store_path))
        print(f"         protection: {token_store.protection_of()}")
        ok, message, _ = token_store.validate_token(config.bot_token)
        message = redact(message, config)
        if ok:
            _ok("telegram getMe", message)
        else:
            _fail("telegram getMe", message)
            problems += 1

    # 3) Writable folders
    print("\n[3] Folders")
    for label, path in (
        ("log folder", config.log_dir),
        ("downloads folder", config.downloads_dir),
        ("jobs folder", config.jobs_file.parent),
    ):
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            _ok(f"{label} writable", str(path))
        except OSError as exc:
            _fail(f"{label} not writable", f"{path}: {exc}")
            problems += 1

    # 4) Dependencies
    print("\n[4] Python packages")
    for module, label in (("telegram", "python-telegram-bot"), ("requests", "requests"), ("PIL", "Pillow")):
        try:
            __import__(module)
            _ok(label)
        except ImportError:
            _fail(label, "missing — run: pip install -r requirements.txt")
            problems += 1
    if HAVE_JSONSCHEMA:
        _ok("jsonschema", "strict schema validation active")
    else:
        _warn("jsonschema missing", "built-in validator is used instead (still strict)")

    # 5) PowerShell
    print("\n[5] PowerShell")
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if exe:
        _ok("powershell found", exe)
    else:
        (_fail if os.name == "nt" else _warn)("powershell not found", "PowerShell actions will fail")
        problems += 1 if os.name == "nt" else 0

    # 6) Planner backends
    print("\n[6] Planner backends")
    info = build_planner(config).status()
    if "cloud" in info:
        cloud = info["cloud"]
        _ok("cloud endpoint configured", f"{cloud['model']} @ {cloud['url']}")
        try:
            import requests

            base = cloud["url"].split("/openai")[0]
            resp = requests.get(f"{base}/models", timeout=10)
            if resp.status_code == 200:
                _ok("cloud reachable", f"HTTP 200 from {base}/models")
            else:
                _warn("cloud answered oddly", f"HTTP {resp.status_code} from {base}/models")
        except Exception as exc:
            _warn("cloud not reachable", str(exc)[:120])
    if "local" in info:
        local = info["local"]
        if local["reachable"] and "ready" in local["detail"].lower():
            _ok("local model ready", local["detail"])
        else:
            _warn("local model not ready", local["detail"])
    _ok("keyword fallback", "always available")

    # 7) Offline planner sanity (never touches the network)
    print("\n[7] Offline planner sanity")
    sample = rule_based_plan("take a screenshot", config.workspace_root)
    _ok("english rules", " | ".join(s.action for s in sample.steps))
    sample_ar = rule_based_plan("خد لقطة شاشة", config.workspace_root)
    _ok("arabic rules", " | ".join(s.action for s in sample_ar.steps))
    print("         (to test the configured chain: python main.py --plan "
          "\"take a screenshot\")")

    # 8) Screen capture
    print("\n[8] Screen capture")
    if importlib.util.find_spec("PIL.ImageGrab") is not None:
        if os.name == "nt":
            _ok("Pillow ImageGrab available")
        else:
            _warn("not on Windows", "screenshots only work on the real Windows machine")
    else:
        _fail("Pillow missing", "pip install Pillow")
        problems += 1

    # 9) Remote desktop (optional; never a reason to fail the check)
    print("\n[9] Remote desktop (/remote, optional)")
    if os.name == "nt":
        _ok("remote control", "available on this Windows PC (approval required for each session)")
    else:
        _warn("remote control", "Windows only; it is refused on this system")
    if (config.tools_dir / "cloudflared.exe").is_file():
        _ok("cloudflared", "present in the tools folder")
    else:
        _warn("cloudflared", "not downloaded yet; it is fetched from the official GitHub release on the first /remote start")
    if shutil.which("ssh"):
        _ok("ssh (fallback tunnel)", "found")
    else:
        _warn("ssh (fallback tunnel)", "not found; Cloudflare remains the only tunnel option")
    _ok("remote limits", f"default {config.remote_default_minutes} min, max {config.remote_max_minutes} min")

    print("\n=== Result ===")
    if problems:
        print(f"{problems} problem(s) found — fix the [FAIL] lines above.")
        return 1
    print("All critical checks passed. Start the bot with: python main.py")
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Startup with retries
# ---------------------------------------------------------------------------

STARTUP_ATTEMPTS = 5
STARTUP_DELAYS = (2, 4, 8, 16)


def run_polling_with_retries(make_application, *, attempts: int = STARTUP_ATTEMPTS,
                             delays: tuple[float, ...] = STARTUP_DELAYS,
                             sleep=None, config: Config | None = None) -> None:
    """Start polling, retrying when Telegram is unreachable at startup.

    python-telegram-bot checks the token (``get_me``) before polling starts, and that
    call is not retried, so a brief network drop used to exit with a fatal error.
    Each attempt builds a fresh Application. A rejected token (``InvalidToken``) is
    not retried. Pending plans are in memory only, so a retry never runs one.
    """
    import time

    from telegram.error import NetworkError

    import asyncio

    sleep = sleep or time.sleep
    for attempt in range(1, attempts + 1):
        application = make_application()
        # run_polling closes the loop it used, so every attempt needs a fresh one.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            application.run_polling(
                allowed_updates=["message", "callback_query"],
                drop_pending_updates=True,
            )
            return
        except NetworkError as exc:
            if attempt >= attempts:
                raise
            delay = delays[min(attempt - 1, len(delays) - 1)]
            message = redact(str(exc), config)
            audit.warning("Telegram not reachable at startup, retrying",
                          attempt=attempt, wait_seconds=delay, error=message)
            print(f"⚠️  Telegram is not reachable right now ({message}). "
                  f"Retrying in {delay} s ({attempt}/{attempts}).", file=sys.stderr)
            sleep(delay)
        finally:
            if not loop.is_closed():
                loop.close()


def main(argv: list[str] | None = None) -> int:
    # Network library debug output can include Telegram URLs with bot tokens.
    # Keep third-party wire logs out of console and application log handlers.
    for logger_name in ("httpx", "httpcore", "urllib3", "telegram"):
        logger = logging.getLogger(logger_name)
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
    audit.install_root_redaction()  # library warnings also pass the token redaction
    ensure_utf8_console()
    enable_dpi_awareness()

    parser = argparse.ArgumentParser(
        description="Personal Windows assistant controlled through a Telegram bot."
    )
    parser.add_argument("--config", default=None, help="path to config.ini (default: ./config.ini)")
    parser.add_argument("--check", action="store_true", help="run environment self checks and exit")
    parser.add_argument("--plan", metavar="TEXT", default=None, help="dry run: print the plan for TEXT")
    parser.add_argument("--no-token-prompt", action="store_true", help="fail instead of asking for a token")
    parser.add_argument("--set-token", action="store_true", help="save/replace the bot token (hidden input)")
    parser.add_argument("--forget-token", action="store_true", help="delete the stored bot token")
    parser.add_argument("--version", action="version", version=f"Telegram PC Assistant {__version__}")
    args = parser.parse_args(argv)

    # --check only reports elevation; every other command refuses to run elevated.
    if not args.check and elevation.is_administrator() is True:
        return _refuse_elevated(args.config)

    if args.set_token:
        return cmd_set_token()
    if args.forget_token:
        return cmd_forget_token()

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"\n❌ Configuration error:\n{exc}\n", file=sys.stderr)
        return 2

    audit.setup(config.log_file, config.log_level)

    if args.check:
        result = self_check(config)
        audit.close()  # release the log file (Windows locks open files)
        return result

    if args.plan:
        result = dry_run(config, args.plan)
        audit.close()
        return result

    # Normal start — first run asks for the token with hidden input.
    if not ensure_token(config, allow_prompt=not args.no_token_prompt):
        print(
            "\n❌ No bot token available, so the assistant cannot start.\n"
            "   Run  python main.py --set-token  (or set TELEGRAM_BOT_TOKEN), then retry.\n",
            file=sys.stderr,
        )
        return 3

    from telegram.error import InvalidToken

    from assistant.bot import build_application

    print("\n" + "=" * 62)
    print(f" Telegram PC Assistant {__version__} is starting (polling mode).")
    print(" Every action needs a ✅ confirmation (button or `approve CODE`).")
    print(f"   Approvers: {sorted(config.approver_user_ids)}")
    if config.planner_mode == "cloud":
        print(" Planner: cloud — request text is sent to pollinations.ai.")
        print("          Set [planner] mode = local for local planning; Telegram still carries chat data.")
    print(" Press Ctrl+C in this window to stop the bot.")
    print("=" * 62 + "\n")
    audit.event(
        "startup", version=__version__, platform=platform.platform(),
        planner=config.planner_mode, token_source=config.token_source,
    )

    try:
        run_polling_with_retries(
            lambda: build_application(config), config=config,
        )
    except KeyboardInterrupt:
        print("\nStopping (Ctrl+C)…")
    except InvalidToken:
        audit.error("Telegram rejected the bot token (InvalidToken)")
        print(
            "\n❌ Telegram rejected the bot token.\n"
            "   Get a fresh one from @BotFather (/mybots → API Token) and run:\n"
            "     python main.py --set-token\n",
            file=sys.stderr,
        )
        return 4
    except Exception as exc:
        message = redact(str(exc), config)
        audit.error(f"Fatal error: {message}", exc_info=True)
        print(f"\n❌ Fatal error: {message}", file=sys.stderr)
        print("   See the log file for details.", file=sys.stderr)
        return 1
    finally:
        audit.event("shutdown")
        audit.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
