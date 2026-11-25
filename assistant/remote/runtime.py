"""Real Windows wiring for a remote session. Imported by the bot; tests inject fakes instead."""

from __future__ import annotations

import shutil
import sys

from ..config import Config
from .banner import RemoteBanner
from .inputs import WindowsBackend
from .session import RemoteDeps
from .stream import MAX_WIDTH, capture_jpeg
from .tunnel import PROVIDER_CLOUDFLARE, PROVIDER_LOCALHOST_RUN, ensure_cloudflared, tunnel_argv


def make_deps(bot, config: Config, chat_id: int) -> RemoteDeps:
    """Bind the session's Telegram output and OS hooks to one private chat."""

    async def say(text: str) -> None:
        await bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)

    async def send_link(text: str) -> None:
        # protect_content asks Telegram to block forwarding and saving of this message.
        await bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True,
                               protect_content=True)

    def argv_for(provider: str, port: int):
        if provider == PROVIDER_CLOUDFLARE:
            if sys.platform != "win32":
                return None
            exe = ensure_cloudflared(config.tools_dir, pinned_sha256=config.cloudflared_sha256)
            return tunnel_argv(provider, port, cloudflared=exe, ssh=None)
        if provider == PROVIDER_LOCALHOST_RUN:
            return tunnel_argv(provider, port, cloudflared=None, ssh=shutil.which("ssh"))
        return None

    return RemoteDeps(
        open_banner=RemoteBanner.open,
        tunnel_argv=argv_for,
        capture=lambda quality: capture_jpeg(quality, MAX_WIDTH),
        input_backend=WindowsBackend,
        say=say,
        send_link=send_link,
        providers=tuple(config.remote_tunnel_order),
    )
