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
