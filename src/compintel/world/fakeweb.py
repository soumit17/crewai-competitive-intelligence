"""Local fake web server for the mock world (spec §1.3): real HTTP, so crewai's built-in
ScrapeWebsiteTool works for real at $0. Serves data/fakeweb on 127.0.0.1:8765 (FAKEWEB_URL).

Directory listings are disabled: an agent can only fetch pages it is given a URL for, so a week-1 scout can't
browse into week-3 folders.
"""

from __future__ import annotations

from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class _Handler(SimpleHTTPRequestHandler):
    def list_directory(self, path):  # noqa: ANN001 - stdlib signature
        self.send_error(HTTPStatus.NOT_FOUND, "Directory listing disabled")
        return None

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass  # quiet by default; serve(verbose=True) restores it

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


class _VerboseHandler(_Handler):
    log_message = SimpleHTTPRequestHandler.log_message


def make_server(
    root: Path, host: str = "127.0.0.1", port: int = 8765, verbose: bool = False
) -> ThreadingHTTPServer:
    if not root.is_dir():
        raise FileNotFoundError(f"{root} not found; run `uv run python scripts/gen_world.py` first")
    handler = partial(_VerboseHandler if verbose else _Handler, directory=str(root))
    return ThreadingHTTPServer((host, port), handler)
