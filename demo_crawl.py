"""demo_crawl.py -- Crawl & sitemap, in depth.

A focused tour of the client-held, turn-based frontier crawler and the sitemap
map, plus the same feature over the HTTP Service API. Runs fully offline: it
serves its own little docs site on localhost.

    env/bin/python demo_crawl.py
"""

from __future__ import annotations

import http.server
import threading
from typing import Any

from webclient import WebClient

# A small "Acme docs" site: the home links into four sections (guide / api /
# pricing / blog), plus an internal page (robots-disallowed) and an external host.
SITE: dict[str, str] = {
    "/": (
        "<h1>Acme Docs</h1>"
        '<a href="/guide">Guide</a> <a href="/api">API reference</a> '
        '<a href="/pricing">Pricing plans</a> <a href="/blog">Blog</a> '
        '<a href="/private">Internal</a> '
        '<a href="https://external.example/x">External site</a>'
    ),
    "/guide": '<h1>Guide</h1><a href="/guide/install">Install</a> '
    '<a href="/guide/usage">Usage</a>',
    "/guide/install": "<h1>Install</h1><p>pip install acme.</p>",
    "/guide/usage": "<h1>Usage</h1><p>import acme; acme.run().</p>",
    "/api": '<h1>API reference</h1><a href="/api/client">Client API</a>',
    "/api/client": "<h1>Client API</h1><p>The full API surface.</p>",
    "/pricing": "<h1>Pricing</h1><p>Our pricing: a free tier and a pro plan.</p>",
    "/blog": '<h1>Blog</h1><a href="/blog/1">Launch</a> <a href="/blog/2">v2 is out</a>',
    "/blog/1": "<h1>Launch</h1><p>We launched today.</p>",
    "/blog/2": "<h1>v2 is out</h1><p>Version 2 ships.</p>",
    "/private": "<h1>Internal</h1><p>staff only.</p>",
}
ROBOTS = "User-agent: *\nDisallow: /private\n"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/robots.txt":
            body, ctype = ROBOTS.encode(), "text/plain"
        elif self.path in SITE:
            title = self.path.strip("/").replace("/", " ").title() or "Home"
            body = (
                f"<html><head><title>{title} · Acme</title></head>"
                f"<body>{SITE[self.path]}</body></html>"
            ).encode()
            ctype = "text/html; charset=utf-8"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002  quiet
        pass


def serve() -> str:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}"


def _rel(url: str | None, base: str) -> str:
    return (url or "").replace(base, "") or "/"


def _purl(page: Any, base: str) -> str:
    """A crawled page's URL (the retained Document), relative to ``base``."""
    return _rel(page.final_url or page.url, base)


def main() -> None:
    base = serve()

    with WebClient() as wc:
        # -- 1. Turn-based: the client manages the frontier, the caller steers ---
        # The client fetches + dedups + scopes; each round YOU (or an LLM) pick
        # which discovered edges to expand next. Nothing auto here.
        print("== turn-based frontier (caller selects each round) ==")
        with wc.crawl(f"{base}/") as crawl:
            crawl.step()  # round 1: fetch the seed, discover its edges
            print("  fetched:  ", [_purl(p, base) for p in crawl.pages])
            print(
                "  frontier: ",
                [(_rel(e.url, base), e.text) for e in crawl.frontier],
            )
            # an agent decides the guide + api sections are what it wants
            picks = [e for e in crawl.frontier if "/guide" in e.url or "/api" in e.url]
            print("  expanding:", [_rel(e.url, base) for e in picks])
            crawl.step(picks)  # round 2: fetch just those
            print("  fetched:  ", [_purl(p, base) for p in crawl.pages])
            print(
                "  frontier: ",
                sorted(_rel(e.url, base) for e in crawl.frontier),
            )

        # -- 2. Auto, best-first by keyword -------------------------------------
        # auto=True self-drives: each round it takes the top-`width` frontier edges
        # scored by keyword relevance (anchor text + URL). "pricing" steers it.
        print("\n== auto crawl, best-first toward 'pricing' (width=2) ==")
        with wc.crawl(
            f"{base}/", auto=True, keywords=["pricing"], width=2, max_pages=5
        ) as crawl:
            crawl.run()
            for p in crawl.pages:  # .pages are lean PageCards (already projected)
                print("  page:     ", _purl(p, base), "|", p.title, "| flags", p.flags)

        # -- 3. Sitemap: an eager, single-domain map ----------------------------
        print("\n== sitemap (eager single-domain crawl) ==")
        smap = wc.sitemap(f"{base}/", depth=2, width=20)
        mapped = sorted(_purl(p, base) for p in smap.pages)
        print("  pages:    ", len(smap.pages))
        print("  urls:     ", mapped)
        print("  external kept out:", "/x" not in " ".join(mapped))
        print("  robots kept /private out:", "/private" not in mapped)

        # -- 4. The same feature over the HTTP Service API ----------------------
        print("\n== Service API: POST /crawl and /sitemap ==")
        from fastapi.testclient import TestClient

        from webclient.service import create_app

        svc = WebClient()
        app = create_app(svc, token="demo")
        auth = {"Authorization": "Bearer demo"}
        with TestClient(app) as api:
            r = api.post(
                "/crawl",
                headers=auth,
                json={"url": f"{base}/", "keywords": ["pricing"], "max_pages": 6},
            )
            data = r.json()
            print(
                f"  POST /crawl -> {r.status_code}:",
                len(data["pages"]),
                "pages, done =",
                data["done"],
            )
            print("    urls:   ", sorted(_rel(u, base) for u in data["urls"]))
            first = data["pages"][0]
            print(
                "    page[0] handle keys:",
                sorted(k for k, v in first.items() if v is not None),
            )
            m = api.post("/sitemap", headers=auth, json={"url": f"{base}/", "depth": 2})
            print(
                "  POST /sitemap ->",
                m.status_code,
                "| urls:",
                sorted(_rel(u, base) for u in m.json()["urls"]),
            )
        svc.close()

    print("\nok")


if __name__ == "__main__":
    main()
