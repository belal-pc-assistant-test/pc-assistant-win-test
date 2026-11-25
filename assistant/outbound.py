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

from typing import Any

from telegram.error import TelegramError
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


class RedactingRequestMixin:
    """Place in front of a request class: ``class Wrapped(RedactingRequestMixin, Base)``."""

    async def do_request(self, url: str, method: str, request_data: RequestData | None = None,
                         **kwargs: Any) -> Any:
        return await super().do_request(  # type: ignore[misc]
            url, method, request_data=prepare_request_data(request_data), **kwargs)


class RedactingHTTPXRequest(RedactingRequestMixin, HTTPXRequest):
    """The request object the bot uses for all Telegram traffic."""
