"""The single choke point for everything the bot sends to Telegram.

Every Bot API call (replies, errors, edits, captions, uploads) goes through
``do_request`` in python-telegram-bot. The classes below wrap that one method,
so no handler has to remember to redact:

* Text parameters (message text, captions, edited text, keyboard labels) have
  every registered secret replaced with ``[REDACTED]`` by
  :func:`assistant.audit.redact_secret`, the same helper the audit log uses.
* An upload is refused if its file name or bytes contain a registered secret,
  and also if the bytes cannot be inspected. Bytes cannot be redacted safely,
  so the send fails with :class:`SecretInFileError` instead.

The URL is not touched. It carries the token, but it is never shown in chat.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from telegram.error import NetworkError, TelegramError
from telegram.request import HTTPXRequest, RequestData
from telegram.request._requestparameter import RequestParameter

from . import audit

#: Bytes read per step when scanning an upload. A secret that straddles two
#: steps is still found, because the scan keeps an overlap of ``len(secret) - 1``.
SCAN_CHUNK = 1 << 20


class SecretInFileError(TelegramError):
    """Raised instead of uploading a file that contains a registered secret."""


def redact_value(value: Any) -> Any:
    """Redact every string inside a request value, including nested lists and dicts."""
    if isinstance(value, str):
        return audit.redact_secret(value)
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        return {redact_value(key): redact_value(item) for key, item in value.items()}
    return value


def _stream_contains_any(stream: Any, needles: list[bytes]) -> bool:
    """Scan a seekable binary stream from its current position, then restore the position."""
    if not all(hasattr(stream, name) for name in ("read", "seek", "tell")):
        raise SecretInFileError("could not inspect the file, so it was not sent.")
    overlap = max(len(needle) for needle in needles) - 1
    start = stream.tell()
    try:
        tail = b""
        while True:
            chunk = stream.read(SCAN_CHUNK)
            if not chunk:
                return False
            if not isinstance(chunk, (bytes, bytearray)):
                raise SecretInFileError("could not inspect the file, so it was not sent.")
            window = tail + bytes(chunk)
            if any(needle in window for needle in needles):
                return True
            tail = window[-overlap:] if overlap > 0 else b""
    except OSError as exc:
        raise SecretInFileError("could not read the file, so it was not sent.") from exc
    finally:
        stream.seek(start)


def _refuse_secret_upload(input_file: Any) -> None:
    secrets = [secret for secret in audit.registered_secrets() if secret]
    if not secrets:
        return
    name = str(getattr(input_file, "filename", "") or "")
    if any(secret in name for secret in secrets):
        raise SecretInFileError("the file name contains the bot token, so the file was not sent.")
    content = getattr(input_file, "input_file_content", None)
    if content is None or isinstance(content, str):
        return  # a file_id or URL: nothing is uploaded from this machine
    needles = [secret.encode("utf-8") for secret in secrets]
    if isinstance(content, (bytes, bytearray)):
        found = any(needle in bytes(content) for needle in needles)
    else:
        found = _stream_contains_any(content, needles)
    if found:
        raise SecretInFileError("the file contains the bot token, so it was not sent.")


def prepare_request_data(request_data: RequestData | None) -> RequestData | None:
    """Return a copy of ``request_data`` with redacted text and no secret-bearing uploads."""
    if request_data is None:
        return None
    parameters = getattr(request_data, "_parameters", None)
    if not isinstance(parameters, list):
        # python-telegram-bot changed its internals. Fail closed rather than
        # send request data that was not checked.
        raise RuntimeError("outbound redaction could not read this request, so nothing was sent.")
    rebuilt: list[RequestParameter] = []
    for param in parameters:
        for input_file in param.input_files or []:
            if input_file is not None:
                _refuse_secret_upload(input_file)
        rebuilt.append(RequestParameter(param.name, redact_value(param.value), param.input_files))
    return RequestData(parameters=rebuilt)


#: Text-only send methods that can be repeated once after a rate limit or a dropped
#: connection. Uploads are not listed: their file stream may already have been read.
RESENDABLE_METHODS = frozenset({"sendMessage", "editMessageText", "editMessageReplyMarkup",
                                "answerCallbackQuery"})
#: Longest wait Telegram's retry time may cause. A longer rate limit is not waited out.
RATE_LIMIT_CAP_SECONDS = 30.0
#: Pause before repeating a send whose connection dropped.
NETWORK_RETRY_SECONDS = 2.0


def _rate_limit_wait(payload: bytes) -> float:
    """Telegram's ``retry_after`` from a 429 body, bounded to a sensible wait."""
    try:
        seconds = float(json.loads(payload)["parameters"]["retry_after"])
    except (ValueError, TypeError, KeyError, AttributeError):
        seconds = 1.0
    return min(max(seconds, 1.0), RATE_LIMIT_CAP_SECONDS)


class RedactingRequestMixin:
    """Place in front of a request class: ``class Wrapped(RedactingRequestMixin, Base)``.

    Every call is redacted first. A text-only send that hits a rate limit (HTTP 429) is
    repeated once after Telegram's retry time, and one that loses its connection is repeated
    once after a short pause. Otherwise a single rate limit or network blip silently drops
    the reply, because most replies are sent directly and not through a retrying helper.
    A second failure is raised as before.
    """

    async def do_request(self, url: str, method: str, request_data: RequestData | None = None,
                         **kwargs: Any) -> Any:
        prepared = prepare_request_data(request_data)
        name = url.rsplit("/", 1)[-1]
        if name not in RESENDABLE_METHODS:
            return await super().do_request(  # type: ignore[misc]
                url, method, request_data=prepared, **kwargs)
        try:
            status, payload = await super().do_request(  # type: ignore[misc]
                url, method, request_data=prepared, **kwargs)
        except NetworkError:
            audit.warning("Telegram send retried once", reason="connection dropped")
            await asyncio.sleep(NETWORK_RETRY_SECONDS)
            return await super().do_request(  # type: ignore[misc]
                url, method, request_data=prepared, **kwargs)
        if status == 429:
            wait = _rate_limit_wait(payload)
            audit.warning("Telegram send retried once", reason="rate limit")
            await asyncio.sleep(wait)
            return await super().do_request(  # type: ignore[misc]
                url, method, request_data=prepared, **kwargs)
        return status, payload


class RedactingHTTPXRequest(RedactingRequestMixin, HTTPXRequest):
    """The request object the bot uses for all Telegram traffic."""
