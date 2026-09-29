"""demo.py -- a tour of the web.* stack as USER STORIES, on the clean DSL.

Mirrors the monolith's demo: build request specs, crawl a site, select + render, record ONE lazy
plan and run it four ways (sync / async / service-blob / remote-server), and onboard a dataset.
Everything high-level goes through the context-managed :class:`~web.dsl.WebClient` ("the WebClient
is just our DSL") and the ``wq`` recorder with typed ``Collection`` / ``Field`` results -- no
hand-built fetcher, no ``try/finally``.

Runs fully offline: it serves its own site on localhost. No browser needed (deterministic).

    env/bin/python packages/demo.py
"""

from __future__ import annotations

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlparse

from web.dsl import WebClient, from_blob, run_blob, wq
from web.fetch import Request
from web.onboard import onboard
from web.resolve import Resolver, flags, paginate_param

_INDEX = b"""<!doctype html><html><head>
  <title>Acme Widgets</title><meta name="description" content="the finest widgets">
  <link rel="canonical" href="/">
  <script type="application/ld+json">{"@type":"Store","name":"Acme"}</script>
</head><body>
  <nav><a href="/">Home</a><a href="/about">About</a></nav>
  <main>
    <h1>Catalog</h1>
    <ul class="products">
      <li class="product"><a class="link" href="/product/1"><span class="name">Widget</span></a><span class="price">$19.99</span></li>
      <li class="product"><a class="link" href="/product/2"><span class="name">Gadget</span></a><span class="price">$34.50</span></li>
      <li class="product"><a class="link" href="/product/3"><span class="name">Gizmo</span></a><span class="price"></span></li>
    </ul>
    <table><tr><th>Region</th><th>Sales</th></tr><tr><td>West</td><td>120</td></tr></table>
  </main>
</body></html>"""
_ABOUT = b"<html><head><title>About</title></head><body><main><p>Acme makes widgets.</p></main></body></html>"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (stdlib name)
        path = urlparse(self.path).path
        if path == "/api":
            body, ctype = json.dumps({"items": [{"sku": "W1"}, {"sku": "G2"}], "next": None}).encode(), "application/json"
        elif path == "/about":
            body, ctype = _ABOUT, "text/html"
        elif path.startswith("/product/"):
            n = path.rsplit("/", 1)[-1]
            body, ctype = f'<html><body><h1 class="sku">SKU-{n}</h1></body></html>'.encode(), "text/html"
        elif path == "/feed":  # a param-paginated dataset (?page=N), 2 pages
            page = int(parse_qs(urlparse(self.path).query).get("page", ["1"])[0])
            body = f'<html><body><main><article class="row">feed p{page}</article></main></body></html>'.encode()
            ctype = "text/html"
        else:
            body, ctype = _INDEX, "text/html"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        pass  # quiet


def _serve() -> "tuple[ThreadingHTTPServer, str]":
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def _h(title: str) -> None:
    print(f"\n\033[1m== {title} ==\033[0m")


# -- the stories -------------------------------------------------------------

def references(base: str) -> None:
    """A Request is a pure spec: build one, derive variants by copy -- no IO."""
    _h("references (pure request specs)")
    spec = Request(url=base + "/?page=1")
    print("url:", spec.url)
    print("with header:", spec.model_copy(update={"headers": {"x-app": "demo"}}).headers)
    print("with cookie:", spec.model_copy(update={"cookies": {"token": "tok"}}).cookies)


async def fetch_and_crawl(wc: WebClient, base: str) -> None:
    """The context-managed WebClient IS the DSL: resolve one page, and crawl the small site."""
    _h("fetch + crawl (WebClient owns the resolver)")
    home = await wc.resolve(base + "/").doc().acollect()   # a resolved Document
    print("resolved:", home.metadata().title, "| flags:", [f.name for f in flags(home)])
    docs = await wc.crawl(base + "/", max_pages=5)
    print("crawled:", sorted((d.url.replace(base, "") or "/") for d in docs))


async def selection_and_render(wc: WebClient, base: str) -> None:
    """Selection + the render surfaces + JSON navigation, off a resolved Document."""
    _h("selection + render")
    home = await wc.resolve(base + "/").doc().acollect()
    for card in home.select_all("li.product"):
        title = card.select(".name")
        price = card.select(".price")
        print("  card:", title.text if title else "?", "|", price.text if price else "?")
    print("table:", home.tables("table"))
    print("markdown:", home.markdown(main_content_only=True).splitlines()[0])
    print("skeleton:", home.skeleton(max_lines=3, drop_chrome=True).replace("\n", " | "))
    api = await wc.resolve(base + "/api").doc().acollect()
    print("json at('items[0].sku'):", api.at("items[0].sku"))


async def sessions(base: str) -> None:
    """A Resolver is a scope (``async with``); ``.session()`` opens a persistent session (cookie jar
    / connection reuse) that survives across resolves and closes with the scope."""
    _h("sessions (a resolver scope + a persistent session)")
    async with Resolver() as rs:
        session = await rs.session()
        a = await session.resolve(base + "/about")
        b = await session.resolve(base + "/")
        print("session reused across:", a.metadata().title, "+", b.metadata().title)


async def pagination(base: str) -> None:
    """Param pagination as a resolver policy: the merged multi-page document."""
    _h("pagination (a resolver policy)")
    async with Resolver(paginate=paginate_param("page", max_pages=2)) as rp:
        paged = await rp.resolve(base + "/feed")
        print("rows across pages:", [e.text for e in paged.select_all("article.row")])


def lazy_plans(base: str) -> str:
    """One expression language: record a chain into a serialisable Plan -- describe it, ship it as a
    blob. Nothing runs. Returns the blob for the evaluator story."""
    _h("lazy plans (record, don't execute)")
    q = (wq.reference(base + "/").resolve().select_all("li.product")
         .extract(name=wq.doc.select(".name").attr("text"),
                  price=wq.doc.select(".price").attr("text").number(default=0),
                  link=wq.doc.select("a.link").attr("href"))
         .filter(wq.doc.select(".price").attr("text") != ""))
    print("describe:", q.describe()[:88], "...")
    print("blob:", q.to_blob()[:72], "...")
    return q.to_blob()


async def evaluator(wc: WebClient, base: str, blob: str) -> None:
    """ONE recorded plan, four dispatch modes -- and eager == lazy. ``collect`` (sync), ``acollect``
    (async), ``to_blob`` -> ``run_blob`` (service / remote server-side)."""
    _h("evaluator (one plan, four dispatch modes)")
    chain = (wq.reference(base + "/").resolve().select_all("li.product")
             .extract(name=wq.doc.select(".name").attr("text"),
                      price=wq.doc.select(".price").attr("text").number(default=0)))
    print("sync  collect :", chain.collect())                    # (1) sync (works even inside a loop)
    print("async acollect:", await chain.acollect())             # (2) async
    print("service run_blob (from lazy_plans' blob):", await run_blob(blob))   # (3) service + (4) remote
    print("blob rebuilt  :", from_blob(blob).describe()[:60], "...")

    # per-element enrichment: follow each product link into its detail page for the SKU
    enriched = await (wq.reference(base + "/").resolve().select_all("li.product")
                      .extract(name=wq.doc.select(".name").attr("text"),
                               link=wq.doc.select("a.link").attr("href"))
                      .extract(sku=wq.doc.reference("link").resolve().select("h1.sku").attr("text"))
                      .acollect())
    print("enriched:", [(r["name"], r["sku"]) for r in enriched])

    # a labelled column with when/then/otherwise + a bound-client chain
    labelled = await (wc.resolve(base + "/").select_all("li.product")
                      .extract(name=wq.doc.select(".name").attr("text"),
                               tier=wq.when(wq.doc.select(".price").attr("text") != "").then("priced").otherwise("free"))
                      .acollect())
    print("when/field via wc:", [(r["name"], r["tier"]) for r in labelled])


class _StubLlm:
    """An offline stand-in for AnthropicLlm: authors the product-row selector once, then done."""

    async def complete(self, prompt: str) -> str:
        if "Rows extracted so far: []" in prompt:
            return 'Use: {"row": "li.product", "fields": {"name": ".name"}}'
        return '{"done": true}'


async def onboard_story(base: str) -> None:
    """The capstone: crawl + author (agent + an Llm) + aggregate into a dataset (the LLM tier)."""
    _h("onboard -- goal -> dataset (crawl + author + aggregate)")
    async with Resolver() as rs:
        result = await onboard("each product's name", base + "/", resolver=rs, llm=_StubLlm(), max_pages=6)
        print(f"pages={result.pages} selection.row={result.selection and result.selection.row!r}")
        print("dataset:", sorted(str(row["name"]) for row in result.rows))


async def main() -> None:
    server, base = _serve()
    try:
        references(base)
        async with WebClient() as wc:  # the DSL entry owns the resolver for the whole session
            await fetch_and_crawl(wc, base)
            await selection_and_render(wc, base)
            await sessions(base)
            await pagination(base)
            blob = lazy_plans(base)
            await evaluator(wc, base, blob)
        await onboard_story(base)
    finally:
        server.shutdown()
    print("\n\033[1mdemo ok\033[0m")


if __name__ == "__main__":
    asyncio.run(main())
