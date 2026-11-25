"""Configuration loading for the Telegram PC assistant.

Everything user specific (Telegram user id, paths, limits, planner choice) lives
in ``config.ini`` next to the project root.

The token is never packaged. Resolution order: project-root token.txt,
TELEGRAM_BOT_TOKEN, per-user store, then the hidden first-run prompt.
"""

from __future__ import annotations

import configparser
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import token_store, audit
from . import app_allowlist

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config.ini"

PLANNER_MODES = ("cloud", "local", "keywords")


class ConfigError(RuntimeError):
    """Raised when the config file is missing or invalid."""


def _split_list(raw: str | None) -> list[str]:
    """Split a comma/semicolon separated config value into a list."""
    if not raw:
        return []
    items: list[str] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk:
            items.append(chunk)
    return items


def _get_bool(section: configparser.SectionProxy, key: str, default: bool) -> bool:
    raw = section.get(key)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on", "enabled"}


def _get_int(section: configparser.SectionProxy, key: str, default: int) -> int:
    raw = section.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(float(raw.strip()))
    except ValueError as exc:
        raise ConfigError(f"[{section.name}] {key} must be a number, got {raw!r}") from exc


def _get_float(section: configparser.SectionProxy, key: str, default: float) -> float:
    raw = section.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"[{section.name}] {key} must be a number, got {raw!r}") from exc


def _get_str(section: configparser.SectionProxy, key: str, default: str) -> str:
    raw = section.get(key)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


IS_WINDOWS = os.name == "nt"


def _expand_short_names(path: Path) -> Path:
    """Windows only: expand 8.3 short names (``RUNNER~1``) to their long form.

    ``Path.resolve()`` follows symlinks/junctions but can still leave 8.3
    short-name components in place, and ``GetLongPathNameW`` only works for
    paths that exist, so walk up to the deepest existing ancestor and
    re-attach the remaining tail.
    """
    if not IS_WINDOWS:
        return path
    try:
        import ctypes
        from ctypes import wintypes

        get_long = ctypes.windll.kernel32.GetLongPathNameW
        get_long.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        get_long.restype = wintypes.DWORD
    except Exception:  # non-Windows build or stripped environment
        return path

    probe = path
    tail: list[str] = []
    while True:
        size = 260
        n = 0
        while True:
            buf = ctypes.create_unicode_buffer(size)
            n = get_long(str(probe), buf, size)
            if n == 0:          # does not exist (yet) or no mapping
                break
            if n <= size:       # buffer was large enough
                break
            size = n            # retry with the size the API asked for
        if n:
            expanded = Path(buf.value[:n])
            for part in reversed(tail):
                expanded = expanded / part
            return expanded
        if probe.parent == probe:  # reached the drive root
            return path
        tail.append(probe.name)
        probe = probe.parent


def normalize_path(path: str | os.PathLike) -> Path:
    """The one canonical form used whenever paths are compared or reported.

    Everything that participates in an allow-list check — configured roots,
    requested paths, download folders — must go through this same function.
    ``Path.resolve`` alone is not enough on Windows: ``C:\\Users\\RUNNER~1\\…``
    and ``C:\\Users\\runneradmin\\…`` are the same folder, but ``resolve()``
    may keep the short form, which made allow-list checks fail spuriously.
    """
    try:
        resolved = Path(path).resolve(strict=False)
    except OSError:  # very long or exotic names
        resolved = Path(os.path.abspath(path))
    if IS_WINDOWS:
        resolved = _expand_short_names(resolved)
    return resolved


def path_is_inside(path: Path, root: Path) -> bool:
    """True when ``path`` is ``root`` itself or below it.

    Path comparison is flavour-aware: ``PureWindowsPath`` compares
    case-insensitively, POSIX paths case-sensitively. Prefix matches are
    boundary-checked by ``relative_to`` (``C:\\allowedx`` is not inside
    ``C:\\allowed``).
    """
    from pathlib import PurePath

    if not isinstance(path, PurePath):
        path = Path(path)
    if not isinstance(root, PurePath):
        root = Path(root)
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_dir(raw: str, base: Path) -> Path:
    """Resolve a config path; relative paths are relative to the project folder."""
    path = Path(os.path.expandvars(os.path.expanduser(raw.strip())))
    if not path.is_absolute():
        path = base / path
    return normalize_path(path)


@dataclass
class Config:
    """Validated runtime configuration."""

    # --- Telegram -------------------------------------------------------
    bot_token: str                  # empty until the first-run setup / env var
    token_source: str               # "file" | "env" | "store" | ""
    token_store_path: Path
    allowed_user_ids: frozenset[int]
    approver_user_ids: frozenset[int]   # must be a subset of allowed_user_ids
    require_distinct_approver: bool

    # --- Planner selection ----------------------------------------------
    planner_mode: str               # "cloud" | "local" | "keywords"
    use_ollama: bool                # is the local backend allowed at all?
    ollama_base_url: str
    ollama_model: str
    ollama_timeout: float
    cloud_url: str
    cloud_model: str
    cloud_timeout: float
    cloud_cooldown: int
    cloud_response_format: bool

    # --- Behaviour ------------------------------------------------------
    workspace_root: Path
    allowed_roots: list[Path]
    command_timeout: int
    max_output_chars: int
    max_file_read_kb: int
    max_plan_steps: int
    max_send_file_mb: int
    protected_processes: list[str]
    screenshot_as_document: bool
    blocked_patterns: list[str]
    log_file: Path
    log_level: str
    keep_screenshots: bool
    screenshots_dir: Path
    downloads_dir: Path
    jobs_file: Path
    voice_model_path: Path
    config_path: Path = field(default=DEFAULT_CONFIG_PATH)

    # --- Remote desktop (/remote) -------------------------------------
    remote_default_minutes: int = 15
    remote_max_minutes: int = 60
    remote_tunnel_order: list[str] = field(default_factory=lambda: ["cloudflare", "localhost.run"])
    cloudflared_sha256: str = ""
    tools_dir: Path = field(default_factory=lambda: PROJECT_DIR / "tools")
    # Owner-added /install and /uninstall entries: name -> winget id (see app_allowlist).
    extra_allowed_apps: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------
    @property
    def log_dir(self) -> Path:
        return self.log_file.parent

    def is_allowed(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.allowed_user_ids

    def is_approver(self, user_id: int | None) -> bool:
        """Only these ids may approve a plan (buttons or ``approve CODE``)."""
        return self.is_allowed(user_id) and user_id in self.approver_user_ids

    @property
    def two_person_rule(self) -> bool:
        """True when the approver is somebody other than the requester."""
        return self.require_distinct_approver

    @property
    def uses_cloud(self) -> bool:
        return self.planner_mode == "cloud"

    def planner_chain(self) -> list[str]:
        """The order of planners that will be tried for each request."""
        if self.planner_mode == "keywords":
            return ["keywords"]
        if self.planner_mode == "local":
            return ["local", "keywords"] if self.use_ollama else ["keywords"]
        chain = ["cloud"]
        if self.use_ollama:
            chain.append("local")
        chain.append("keywords")
        return chain


def _resolve_token() -> tuple[str, str]:
    """Use project-root token.txt first, then environment, then stored token.

    Empty files fall through. A non-empty invalid token is rejected by the caller;
    unreadable or invalid UTF-8 files fail closed rather than silently falling back.
    The file is never modified. Config location does not change the project root.
    """
    path = PROJECT_DIR / "token.txt"
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                value = line.strip().lstrip("\ufeff").strip()
                if value:
                    return value, "file"
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError) as exc:
        raise ConfigError("Cannot read project-root token.txt as UTF-8. Check file permissions and encoding.") from exc

    env = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if env:
        return env, "env"
    stored = token_store.load_stored_token()
    if stored:
        return stored, "store"
    return "", ""


def load_config(path: str | Path | None = None) -> Config:
    """Read and validate ``config.ini``.

    A missing bot token is **not** an error any more: the caller (main.py) starts
    the first-run setup when ``config.bot_token`` is empty.
    """
    config_path = Path(path).expanduser().resolve() if path else DEFAULT_CONFIG_PATH
    if not config_path.is_file():
        raise ConfigError(
            f"Config file not found: {config_path}\n"
            "Copy config.example.ini to config.ini (install.bat does it for you)."
        )

    parser = configparser.ConfigParser(interpolation=None)
    try:
        with config_path.open("r", encoding="utf-8-sig") as fh:
            parser.read_file(fh)
    except (OSError, configparser.Error) as exc:
        raise ConfigError(f"Could not read {config_path}: {exc}") from exc

    tg = parser["telegram"] if "telegram" in parser else None
    if tg is None:
        raise ConfigError("Missing [telegram] section in config.ini")

    bot_token, token_source = _resolve_token()
    audit.register_secret(bot_token)
    if bot_token and not token_store.valid_token_format(bot_token):
        raise ConfigError(
            "The bot token found in "
            f"{'token.txt' if token_source == 'file' else 'the TELEGRAM_BOT_TOKEN environment variable' if token_source == 'env' else 'the stored token'} "
            "does not look like a Telegram token. Correct or remove the invalid source."
        )

    raw_ids = _split_list(tg.get("allowed_user_ids"))
    user_ids: set[int] = set()
    for raw in raw_ids:
        try:
            user_ids.add(int(raw))
        except ValueError as exc:
            raise ConfigError(
                f"[telegram] allowed_user_ids contains a non-numeric value: {raw!r}"
            ) from exc
    if not user_ids:
        raise ConfigError(
            "[telegram] allowed_user_ids is empty. Put YOUR numeric Telegram user id "
            "there, otherwise the bot ignores every message."
        )

    raw_approvers = _split_list(tg.get("approver_user_ids"))
    approver_ids: set[int] = set()
    for raw in raw_approvers:
        try:
            approver_ids.add(int(raw))
        except ValueError as exc:
            raise ConfigError(
                f"[telegram] approver_user_ids contains a non-numeric value: {raw!r}"
            ) from exc
    if not approver_ids:
        approver_ids = set(user_ids)   # default: the same people
    if not approver_ids <= user_ids:
        raise ConfigError('[telegram] approver_user_ids must also be included in allowed_user_ids.')
    distinct = _get_bool(tg, 'require_distinct_approver', False)
    if distinct and len(user_ids) < 2:
        raise ConfigError('[telegram] require_distinct_approver needs at least two allowed user IDs.')

    ollama = parser["ollama"] if "ollama" in parser else parser["DEFAULT"]
    planner_section = parser["planner"] if "planner" in parser else parser["DEFAULT"]
    behaviour = parser["behaviour"] if "behaviour" in parser else parser["DEFAULT"]
    paths = parser["paths"] if "paths" in parser else parser["DEFAULT"]
    voice = parser["voice"] if "voice" in parser else parser["DEFAULT"]

    planner_mode = _get_str(planner_section, "mode", "cloud").lower()
    if planner_mode not in PLANNER_MODES:
        raise ConfigError(
            f"[planner] mode must be one of {', '.join(PLANNER_MODES)} — got {planner_mode!r}"
        )

    workspace_root = _resolve_dir(_get_str(paths, "workspace_root", "."), PROJECT_DIR)
    allowed_roots = [_resolve_dir(p, PROJECT_DIR) for p in _split_list(paths.get("allowed_roots"))]

    downloads_dir = _resolve_dir(_get_str(paths, "downloads_dir", "downloads"), PROJECT_DIR)
    jobs_file = _resolve_dir(_get_str(paths, "jobs_file", "data/jobs.json"), PROJECT_DIR)
    log_file = _resolve_dir(_get_str(paths, "log_file", "logs/assistant.log"), PROJECT_DIR)
    screenshots_dir = _resolve_dir(
        _get_str(paths, "screenshots_dir", "screenshots"), PROJECT_DIR
    )

    apps_section = parser["apps"] if "apps" in parser else parser["DEFAULT"]
    try:
        extra_apps = app_allowlist.parse_extra(
            apps_section.get("extra_allowed_apps"), reserved=app_allowlist.load_builtin().keys())
    except ValueError as exc:
        raise ConfigError(f"[apps] extra_allowed_apps: {exc}") from exc

    remote = parser["remote"] if "remote" in parser else parser["DEFAULT"]
    remote_default = _get_int(remote, "default_minutes", 15)
    remote_max = _get_int(remote, "max_minutes", 60)
    if not 1 <= remote_max <= 60:
        raise ConfigError("[remote] max_minutes must be between 1 and 60.")
    if not 1 <= remote_default <= remote_max:
        raise ConfigError("[remote] default_minutes must be between 1 and max_minutes.")
    tunnel_order = [item.lower() for item in _split_list(remote.get("tunnel_order"))] or ["cloudflare", "localhost.run"]
    unknown_providers = [item for item in tunnel_order if item not in ("cloudflare", "localhost.run")]
    if unknown_providers:
        raise ConfigError(f"[remote] tunnel_order has unknown providers: {', '.join(unknown_providers)}")
    pinned = _get_str(remote, "cloudflared_sha256", "").lower()
    if pinned and not re.fullmatch(r"[0-9a-f]{64}", pinned):
        raise ConfigError("[remote] cloudflared_sha256 must be 64 hexadecimal characters or empty.")

    return Config(
        bot_token=bot_token,
        token_source=token_source,
        token_store_path=token_store.token_path(),
        allowed_user_ids=frozenset(user_ids),
        approver_user_ids=frozenset(approver_ids),
        require_distinct_approver=distinct,
        planner_mode=planner_mode,
        use_ollama=_get_bool(ollama, "use_ollama", True),
        ollama_base_url=_get_str(ollama, "base_url", "http://127.0.0.1:11434").rstrip("/"),
        ollama_model=_get_str(ollama, "model", "qwen2.5:3b"),
        ollama_timeout=_get_float(ollama, "timeout", 90.0),
        cloud_url=_get_str(
            planner_section, "cloud_url", "https://text.pollinations.ai/openai"
        ),
        cloud_model=_get_str(planner_section, "cloud_model", "openai"),
        cloud_timeout=_get_float(planner_section, "cloud_timeout", 45.0),
        cloud_cooldown=_get_int(planner_section, "cloud_cooldown_seconds", 120),
        cloud_response_format=_get_bool(planner_section, "cloud_response_format", True),
        workspace_root=workspace_root,
        allowed_roots=allowed_roots,
        command_timeout=_get_int(behaviour, "command_timeout", 60),
        max_output_chars=_get_int(behaviour, "max_output_chars", 3500),
        max_file_read_kb=_get_int(behaviour, "max_file_read_kb", 200),
        max_plan_steps=_get_int(behaviour, "max_plan_steps", 6),
        max_send_file_mb=_get_int(behaviour, "max_send_file_mb", 45),
        protected_processes=_split_list(behaviour.get("protected_processes")),
        screenshot_as_document=_get_bool(behaviour, "screenshot_as_document", True),
        blocked_patterns=_split_list(behaviour.get("blocked_patterns")),
        log_file=log_file,
        log_level=_get_str(behaviour, "log_level", "INFO").upper(),
        keep_screenshots=_get_bool(paths, "keep_screenshots", True),
        screenshots_dir=screenshots_dir,
        downloads_dir=downloads_dir,
        jobs_file=jobs_file,
        voice_model_path=_resolve_dir(_get_str(voice, "model_path", "models/faster-whisper-tiny"), PROJECT_DIR),
        config_path=config_path,
        remote_default_minutes=remote_default,
        remote_max_minutes=remote_max,
        remote_tunnel_order=tunnel_order,
        cloudflared_sha256=pinned,
        tools_dir=_resolve_dir(_get_str(remote, "tools_dir", "tools"), PROJECT_DIR),
        extra_allowed_apps=extra_apps,
    )
