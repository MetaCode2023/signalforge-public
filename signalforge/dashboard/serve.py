"""Serve the built dashboard on loopback only."""

from __future__ import annotations

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        return


def make_server(directory, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    if host not in LOOPBACK_HOSTS:
        raise ValueError(
            "the dashboard serves loopback only (127.0.0.1, localhost or ::1); "
            "binding to every interface would expose the operator dashboard to the network"
        )
    handler = partial(QuietHandler, directory=str(Path(directory)))
    return ThreadingHTTPServer((host, port), handler)


def serve_forever(directory, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = make_server(directory, host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
