"""A self-contained tour of the web.* stack -- run it: ``env/bin/python packages/demo.py``.

Spins up a tiny local site (stdlib threaded HTTP server, ephemeral port), then exercises each layer
against it, bottom-up, printing what each produces:

    fetch    -- Request -> Snapshot (raw transport)
    parse    -- bytes -> Document: records(), skeleton() marks, tables(), metadata(), JSON at()
    resolve  -- Request -> Document with policy: flags() conclusions + param pagination
    crawl    -- Goal -> Documents (canonical-deduped frontier)
    dsl      -- a lazy plan: ref(url).doc().select_all(...).project(...).number() in 3 dispatch modes

No browser needed (deterministic); the browser backend + interaction agent are covered by tests.
"""

from __future__ import annotations

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from web.crawl import Crawler, Goal
from web.dsl import DSL, run_blob
from web.fetch import HttpFetcher, Request
from web.parse import parse
from web.resolve import Resolver, flags, paginate_param

_INDEX = b"""<!doctype html><html><head>
  <title>Acme Widgets</title>
  <meta name="description" content="the finest widgets">
  <link rel="canonical" href="/">
  <script type="application/ld+json">{"@type":"Store","name":"Acme"}</script>
</head><body>
  <nav><a href="/">Home</a><a href="/about">About</a></nav>
  <main>
    <h1>Catalog</h1>
    <ul class="products">
      <li class="product"><span class="name">Widget</span><span class="price">$19.99</span></li>
      <li class="product"><span class="name">Gadget</span><span class="price">$34.50</span></li>
      <li class="product"><span class="name">Gizmo</span><span class="price">$8.00</span></li>
    </ul>
    <table><tr><th>Region</th><th>Sales</th></tr><tr><td>West</td><td>120</td></tr></table>
  </main>
</body></html>"""
_ABOUT = b"<html><head><title>About</title></head><body><p>Acme makes widgets.</p></body></html>"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (stdlib name)
        path = self.path.split("?")[0]
        if path == "/api":
            body, ctype = json.dumps({"items": [{"sku": "W1"}, {"sku": "G2"}], "next": None}).encode(), "application/json"
        elif path == "/about":
            body, ctype = _ABOUT, "text/html"
        else:
            body, ctype = _INDEX, "text/html"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        pass  # quiet


def _serve() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def _h(title: str) -> None:
    print(f"\n\033[1m== {title} ==\033[0m")


async def main() -> None:
    server, base = _serve()
    try:
        _h("fetch -- Request -> Snapshot")
        f = HttpFetcher()
        snap = await f.fetch(Request(url=base + "/"))
        print(f"status={snap.status} bytes={len(snap.content)} content-type={snap.headers.get('content-type')}")
        await f.aclose()

        _h("parse -- bytes -> Document")
        doc = parse(snap.content, content_type="text/html", url=base + "/")
        print("records:", [(r.item_selector, r.count) for r in doc.records()])
        print("metadata:", doc.metadata().title, "|", doc.metadata().description, "| ld:", doc.metadata().ld_json)
        print("tables:", doc.tables())
        print("skeleton (marked):")
        for line in doc.skeleton(drop_chrome=True).splitlines():
            print("   ", line)
        api = parse((await (fj := HttpFetcher()).fetch(Request(url=base + "/api"))).content, content_type="application/json")
        await fj.aclose()
        print("json at('items[0].sku'):", api.at("items[0].sku"))

        _h("resolve -- Request -> Document + flags")
        r = Resolver()
        rdoc = await r.resolve(Request(url=base + "/"))
        print("flags:", [(fl.name, fl.remedy, round(fl.confidence, 2)) for fl in flags(rdoc)])
        await r.aclose()
        rp = Resolver(paginate=paginate_param("page", max_pages=2))
        paged = await rp.resolve(Request(url=base + "/"))
        print("paginated bytes (2 pages merged):", len(paged.content))
        await rp.aclose()

        _h("crawl -- Goal -> Documents")
        c = Crawler(Resolver())
        urls = [d.url async for d in c.crawl(Goal(start=base + "/", max_pages=5))]
        print("crawled:", sorted(u.replace(base, "") or "/" for u in urls))
        await c.aclose()

        _h("dsl -- one lazy plan, three dispatch modes")
        d = DSL(Resolver())
        try:
            q = d.ref(base + "/").doc().select_all("li.product").project(name=".name", price=".price").number(field="price")
            rows = await q.acollect()  # async dispatch
            print("acollect:", rows)
            blob = d.ref(base + "/").doc().select_all("li.product").project(name=".name").to_blob()  # API dispatch
            print("to_blob:", blob[:70], "...")
            server_rows = await run_blob(blob, Resolver())  # remote dispatch (server side)
            print("run_blob:", server_rows)
        finally:
            await d.aclose()
    finally:
        server.shutdown()
    print("\n\033[1mdemo ok\033[0m")


if __name__ == "__main__":
    asyncio.run(main())
