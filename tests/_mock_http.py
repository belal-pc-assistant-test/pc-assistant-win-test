"""Shared local HTTP mock for the tests (not a test module).

Why this exists: on Windows, a client that gives up early (a timeout, a retry, a
test that ends) makes the server's next write fail with ConnectionAbortedError
(WinError 10053) or ConnectionResetError (WinError 10054). An uncaught error in a
handler prints a traceback and can leave the connection half-closed. Every mock
here therefore:
  * runs on ThreadingHTTPServer with daemon threads, so one stuck or dropped
    connection cannot block the others or the server shutdown;
  * sends responses through write_safely(), which ignores OSError from a client
    that already went away; and
  * wraps handle(), so an OSError raised while reading or writing never reaches
    the server's error printer.
"""

from __future__ import annotations

import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

# A URL that must never be contacted. It has no DNS entry and no listener. Tests that use it
# patch requests (refuse_connections_except), so no name lookup or socket happens. If a test
# forgets the patch, the hermetic guard refuses the lookup instead.
DEAD_URL = "http://no-server.invalid"


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        return

    def write_safely(self, data: bytes) -> bool:
        """Send bytes to the client. A client that has gone away is not an error here."""
        try:
            self.wfile.write(data)
            return True
        except OSError:
            return False

    def handle(self) -> None:
        try:
            super().handle()
        except OSError:
            # The connection was reset or aborted while reading or writing. There is
            # nobody left to answer, so drop it quietly.
            pass

    def reset_connection(self) -> None:
        """Close with RST (SO_LINGER 0), so the client sees a reset, not a clean end."""
        try:
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        except OSError:
            pass
        self.close_connection = True
        try:
            self.connection.close()
        except OSError:
            pass


def start_server(handler_cls: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def stop_server(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()


def refused_error(url: str = "") -> Exception:
    """The error requests raises when nothing accepts the connection.

    Built in memory, so it behaves the same on every OS. A real closed port is not used:
    Linux refuses at once, but Windows can spend about 2 seconds on SYN retries first.
    """
    import requests
    return requests.exceptions.ConnectionError(
        ConnectionRefusedError(10061, f"connection refused {url}".strip()))


def refuse_connections_except(testcase, allowed_prefix: str) -> None:
    """Patch requests.get and requests.post for one test.

    A URL that starts with allowed_prefix goes to the real function (the test's own mock
    server). Any other URL raises refused_error() without opening a socket.
    """
    import requests

    real = {"get": requests.get, "post": requests.post}

    def router(name: str):
        def route(url, *args, **kwargs):
            if str(url).startswith(allowed_prefix):
                return real[name](url, *args, **kwargs)
            raise refused_error(str(url))
        return route

    for name in ("get", "post"):
        patcher = mock.patch(f"requests.{name}", side_effect=router(name))
        patcher.start()
        testcase.addCleanup(patcher.stop)
