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

from .fixtures import FIXTURES, LAB_CSS, Fixture, brand, landing

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


# fixtures grouped for the index, in this order (matched on the feature's prefix before ':')
_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Records & extraction", ("extract", "fetch", "metadata")),
    ("Detection · signals → flags", ("signals",)),
    ("Pagination", ("pagination",)),
    ("Interaction, network & the browser", ("interact", "network")),
    ("Document kinds", ("kind",)),
    ("Transport, auth & resiliency", ("transport", "errors", "resiliency", "auth")),
    ("Discovery & crawl", ("crawl",)),
)


def _category(feature: str) -> str:
    head = feature.split(":")[0]
    for label, prefixes in _CATEGORIES:
        if head in prefixes:
            return label
    return "Other"


_INDEX_CSS = """<style>
.lab-head{max-width:48rem}
.lab-notes{display:grid;gap:.6rem;grid-template-columns:repeat(auto-fit,minmax(15rem,1fr));margin:1rem 0 2rem}
.lab-note{border:1px solid var(--line);border-radius:var(--radius);background:var(--card);padding:.8rem .95rem}
.lab-note h3{margin:0 0 .25rem;font-size:.92rem}
.lab-note p{margin:0;color:var(--muted);font-size:.88rem}
.lab-search{width:100%;max-width:22rem;margin:.5rem 0 1.5rem}
.lab-grid{display:grid;gap:.7rem;grid-template-columns:repeat(auto-fill,minmax(17rem,1fr))}
.lab-item{display:block;border:1px solid var(--line);border-radius:var(--radius);background:var(--card);padding:.7rem .85rem;color:inherit}
.lab-item:hover{border-color:var(--accent);text-decoration:none}
.lab-item .nm{font-weight:650;color:inherit}
.lab-item .nm:hover{color:var(--accent)}
.lab-item .ds{color:var(--muted);font-size:.86rem;margin:.15rem 0 .45rem}
.lab-item .ft{font-family:ui-monospace,Menlo,monospace;font-size:.72rem;color:var(--chip-fg);background:var(--chip);padding:.03rem .4rem;border-radius:999px}
.lab-item .exp{font-size:.78rem}
.lab-count{color:var(--muted);font-weight:400;font-size:.8rem}
.lab-cat{scroll-margin-top:1rem}
</style>"""


def _index_html() -> bytes:
    groups: dict[str, list[Fixture]] = {}
    for f in FIXTURES.values():
        groups.setdefault(_category(f.feature), []).append(f)
    order = [label for label, _ in _CATEGORIES] + [g for g in groups if g not in dict(_CATEGORIES)]

    def card(f: Fixture) -> str:
        badge = ' <span class="lab-badge">browser</span>' if f.browser else ""
        return (f'<div class="lab-item" data-q="{f.name} {f.title} {f.feature}">'
                f'<a class="nm" href="{f.path}">{f.name}</a>{badge}'
                f'<div class="ds">{f.title}</div>'
                f'<span class="ft">{f.feature}</span> '
                f'<a class="exp" href="/lab/{f.name}.json">expected →</a></div>')

    sections = "".join(
        f'<section class="lab-cat" id="{label.split()[0].lower()}"><h2>{label} '
        f'<span class="lab-count">{len(groups[label])}</span></h2>'
        f'<div class="lab-grid">{"".join(card(f) for f in groups[label])}</div></section>'
        for label in order if label in groups
    )

    notes = "".join(f'<div class="lab-note"><h3>{h}</h3><p>{p}</p></div>' for h, p in (
        ("Everything is a plan",
         "An op records a wire-safe, typed plan; <code>RUN(t)=fold(plan, events)</code>. "
         "The same plan runs sync, async or remote — dispatch modes, not three code paths."),
        ("Tiers escalate on evidence",
         "A cheap static fetch first; the client goes to a real browser only when signals say it "
         "must (a JS-gated SPA, a consent wall, shadow DOM)."),
        ("Signals → flags",
         "Tiered, confidence-scored signals (evidence) combine into flags (conclusions: spa, "
         "login_required, pagination, cookie_banner …) that auto-remediation and the pipeline act on."),
        ("One bounded loop",
         "Pagination, crawl, extract and interaction all derive from one observe → decide → apply loop."),
        ("Traceable by construction",
         "Each step stamps events — rrweb DOM, network facts correlated to the DOM, pool leases — "
         "so a run can be watched as it unfolds and replayed later."),
        ("The lab is the contract",
         "Every fixture publishes its expected result as JSON. Tests, demos and docs all assert "
         "against one set of facts — the website can run the same suite (LAB_URL)."),
    ))

    body = f"""
<nav>{brand("lab")}<a href="/">home</a><a href="/lab/index.json">index.json</a></nav>
<div class="lab-head">
<h1>The lab</h1>
<p>One offline site that is the package's torture test, demo site and docs playground. Each
page under <code>/lab/&lt;name&gt;</code> strains one feature and publishes its expected result
at <code>/lab/&lt;name&gt;.json</code>.</p>
</div>
<div class="lab-notes">{notes}</div>
<input class="lab-search" type="search" placeholder="filter fixtures… (name, feature)" oninput="labFilter(this.value)">
{sections}
<footer>{len(FIXTURES)} fixtures · every one has an expected result</footer>
<script>
function labFilter(q){{
  q = (q||'').trim().toLowerCase();
  document.querySelectorAll('.lab-item').forEach(function(el){{
    el.style.display = !q || (el.getAttribute('data-q')||'').toLowerCase().indexOf(q) >= 0 ? '' : 'none';
  }});
  document.querySelectorAll('.lab-cat').forEach(function(sec){{
    var any = [].some.call(sec.querySelectorAll('.lab-item'), function(el){{ return el.style.display !== 'none'; }});
    sec.style.display = any ? '' : 'none';
  }});
}}
</script>"""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>webclient lab</title>{LAB_CSS}{_INDEX_CSS}</head><body>{body}</body></html>").encode()


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
