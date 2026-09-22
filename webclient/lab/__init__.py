"""The LAB: one offline site that is the package's torture test, demo site and docs
playground (roadmap N13, decision D6). Every fixture page under ``/lab/<name>`` strains one
feature -- a static record list, a JS-gated SPA, an XHR-backed feed, pagination (rel=next,
page params, a cursor API), tabs, shadow DOM, an iframe, a login wall, an anti-bot
interstitial, a redirect chain, JSON / XML (RSS) / PDF documents, a sitemap + robots, a
large document, a live interactive app, a slow endpoint, an error page -- and every
fixture publishes its EXPECTED result as JSON at ``/lab/<name>.json`` so tests, demos and
docs all assert against the same facts. ``/lab/index.json`` lists them.

    from webclient.lab import serve
    base = serve()                      # a random localhost port, in a daemon thread
    wc.fetch(f"{base}/lab/shop")

``python -m webclient.lab [port]`` serves it for a browser / the docs playground. Pure
stdlib (``http.server``): no dependency, so it runs under the base install.
"""

from __future__ import annotations

import http.server
import json
import threading
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .fixtures import FIXTURES, Fixture, landing

__all__ = ["serve", "FIXTURES", "Fixture", "LabServer"]


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "webclient-lab/1"

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def _handle(self, method: str) -> None:
        url = urlsplit(self.path)
        path, query = url.path, parse_qs(url.query)
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        status, headers, out = route(method, path, query, dict(self.headers.items()), body)
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, format: str, *args: Any) -> None:  # quiet
        pass


def route(
    method: str, path: str, query: dict[str, list[str]], headers: dict[str, str], body: bytes
) -> tuple[int, dict[str, str], bytes]:
    """Resolve one request to ``(status, headers, body)`` -- the lab's whole router, pure so
    it can be unit-tested without a socket."""
    if path in ("/", "/index.html"):
        return 200, {"Content-Type": "text/html; charset=utf-8"}, landing()
    if path == "/lab" or path == "/lab/":
        return 200, {"Content-Type": "text/html; charset=utf-8"}, _index_html()
    if path == "/lab/index.json":
        return 200, {"Content-Type": "application/json"}, json.dumps(
            [{"name": f.name, "title": f.title, "path": f.path, "feature": f.feature,
              "browser": f.browser} for f in FIXTURES.values()]).encode()
    for fixture in FIXTURES.values():
        if path == f"/lab/{fixture.name}.json":
            return 200, {"Content-Type": "application/json"}, json.dumps(fixture.expected).encode()
        resp = fixture.handle(method, path, query, headers, body)
        if resp is not None:
            return resp
    return 404, {"Content-Type": "text/plain"}, b"not found"


def _index_html() -> bytes:
    rows = "".join(
        f'<li><a href="{f.path}">{f.name}</a> — {f.title} <small>({f.feature}'
        f'{", browser" if f.browser else ""})</small> · <a href="/lab/{f.name}.json">expected</a></li>'
        for f in FIXTURES.values()
    )
    return (f"<html><head><title>webclient lab</title></head><body><h1>Lab</h1>"
            f"<p>One fixture per feature; each publishes its expected result.</p><ul>{rows}</ul>"
            f"</body></html>").encode()


class LabServer:
    """The lab bound to a port (a daemon thread); ``base`` is its URL."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._server = http.server.ThreadingHTTPServer((host, port), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="webclient-lab")
        self._thread.start()
        self.base = f"http://{host}:{self._server.server_port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> "LabServer":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


_SHARED: "LabServer | None" = None


def serve() -> str:
    """Start (once per process) the lab on a random localhost port and return its base URL."""
    global _SHARED
    if _SHARED is None:
        _SHARED = LabServer()
    return _SHARED.base
