"""demo.py -- one clean tour of every implemented webclient feature.

Maintained with every milestone. Sections marked [M<n>]/[P<n>] appear as
their milestone lands.

Runs fully offline: it serves its own demo site on localhost.

    env/bin/python demo.py
"""

from __future__ import annotations

import http.server
import threading
from typing import Any

from webclient import (
    RETURN,
    DOMUpdateEvent,
    Event,
    HtmlBacking,
    NavigationEvent,
    WebClient,
    from_blob,
    from_url,
    wq,
)

PAGE = b"""
<html><head><title>Demo Shop</title></head><body>
<nav><a href="/about">about</a></nav>
<main>
  <h1>Featured Items</h1>
  <p>Hand-picked <strong>daily</strong>.</p>
  <div class="card"><h2 class="title">Aeropress</h2>
    <a class="link" href="/items/1">view</a><span class="price">$39</span></div>
  <div class="card"><h2 class="title">Grinder</h2>
    <a class="link" href="/items/2">view</a><span class="price">$129</span></div>
</main>
<footer>fine print</footer>
</body></html>
"""
ITEM = b'{"id": %d, "name": "%s", "stock": {"count": 7}}'
# a JS-gated page: the server sends an empty shell; a script injects the content,
# so a static fetch sees nothing and a browser render sees the paragraph.
SPA = b"""
<html><head><title>SPA</title></head><body>
  <div id="app"></div>
  <script>
    document.getElementById("app").innerHTML =
      "<p>" + Array(60).fill("client-rendered content").join(" ") + "</p>";
  </script>
</body></html>
"""
APP = b"""
<html><head><title>Live App</title></head><body>
  <h1>Cart</h1>
  <input id="qty" type="text">
  <button id="add" onclick="document.querySelector('#cart').insertAdjacentHTML(
    'beforeend', '<li>item x' + document.querySelector('#qty').value + '</li>')">add</button>
  <ul id="cart"></ul>
  <script>console.log("app ready");</script>
</body></html>
"""

# a search-engine results page (DuckDuckGo-shaped): three hits, one an ad row.
SEARCH = b"""
<html><body>
  <div class="result result--ad"><span>Sponsored</span></div>
  <div class="result">
    <a class="result__a" href="https://example.com/aeropress">Aeropress Guide</a>
    <a class="result__snippet">How to brew a great cup with an Aeropress.</a>
  </div>
  <div class="result">
    <a class="result__a" href="https://example.com/grinder">Best Burr Grinders</a>
    <a class="result__snippet">A roundup of burr grinders for espresso.</a>
  </div>
  <div class="result">
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fkettle">Gooseneck Kettles</a>
    <a class="result__snippet">Pouring control for pour-over coffee.</a>
  </div>
</body></html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/" or self.path.startswith("/?"):  # html (query ignored)
            body, ctype = PAGE, "text/html; charset=utf-8"
        elif self.path == "/items/1":  # json documents
            body, ctype = ITEM % (1, b"Aeropress"), "application/json"
        elif self.path == "/items/2":
            body, ctype = ITEM % (2, b"Grinder"), "application/json"
        elif self.path.startswith("/feed"):  # paginated + cookie-aware
            from urllib.parse import parse_qs, urlparse

            page = int(parse_qs(urlparse(self.path).query).get("p", ["1"])[0])
            user = (
                "friend"
                if "token=tok" in (self.headers.get("Cookie") or "")
                else "guest"
            )
            nxt = (
                f'<a class="next" href="/feed?p={page + 1}">more</a>'
                if page < 3
                else ""
            )
            body = (
                f"<html><body><h1>feed p{page} for {user}</h1>{nxt}" "</body></html>"
            ).encode()
            ctype = "text/html"
        elif self.path == "/login":  # sets a session cookie
            self.send_response(200)
            self.send_header("Set-Cookie", "token=tok; Path=/")
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"welcome")
            return
        elif self.path.startswith("/search"):  # a search-engine results page
            body, ctype = SEARCH, "text/html; charset=utf-8"
        elif self.path == "/spa":  # a JS-gated page (content injected by script)
            body, ctype = SPA, "text/html"
        elif self.path == "/app":  # a JS-driven live page
            body, ctype = APP, "text/html"
        elif self.path == "/old":  # a redirect hop
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"lost")
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


def main() -> None:
    base = serve()

    # [M1] References are pure request specs -- build, derive, inspect.
    spec = from_url(f"{base}/?utm=x", params={"page": "1"})
    print("url:        ", spec.url)
    print("derived:    ", spec.with_params(page="2").replace(fragment="top").url)
    print("joined:     ", spec.join("items/1").url)

    # [M2] A WebClient owns the pool, bus, plugins; it is the lifecycle root.
    with WebClient(default_headers={"user-agent": "webclient-demo"}) as wc:

        # [M2] Live event stream: everything observable crosses one bus.
        def _on_network(e: Event) -> None:
            code = getattr(e, "status_code", None)
            path = getattr(getattr(e, "request", None), "path", "")
            print(f"event:       {e.topic} #{e.seq} {code} {path}")

        wc.bus.subscribe("network", _on_network)

        # [M2] Fetch through a redirect; loud by default, optional=True lenient.
        shop = wc.ref(f"{base}/old").resolve()
        print("final url:  ", shop.final_url)
        missing = wc.ref(f"{base}/nope").resolve(error=RETURN)
        print("optional:   ", missing.status_code, "ok:", missing.ok)

        # [M2] Search: a client verb returning structured hits (title/url/desc),
        #      built on the interface itself (fetch + select). DDG by default;
        #      point it at the demo's own results page here.
        hits = wc.search("coffee", endpoint=f"{base}/search", limit=2)
        print("search:     ", [(h.rank, h.title, h.url) for h in hits])

        # [M2] Crawl: a client-held, scoped traversal used as a context manager.
        #      The client manages the frontier (dedup/scope); the caller steers a
        #      round (crawl.step(select)) or lets it self-drive (auto). Output is an
        #      LLM-efficient .summary() per page + the unresolved frontier edges --
        #      resource links dropped, and scored + sorted by importance (nav /
        #      "read more" / article links high, footer / legal / social low).
        with wc.crawl(f"{base}/feed", auto=True, max_pages=4, browser=False) as crawl:
            crawl.step()  # one turn: fetch the seed, discover its edges
            print("frontier:   ", [(round(e.score, 2), e.url.replace(base, ""))
                                    for e in crawl.frontier])
            crawl.run()   # then let it self-drive the rest
            print("crawled:    ", [p.transport.final_url.replace(base, "")
                                    for p in crawl.pages if p.transport])
        # sitemap: an eager, single-domain crawl -> pages + edges (site map)
        smap = wc.sitemap(f"{base}/", depth=1, width=10)
        print("sitemap:    ", len(smap.pages), "pages,", len(smap.frontier), "edges")

        # [P1] Every object is addressable: short scoped names, a root chain
        #      (ref -> doc), recovery by name from the resolver, shop.ref().
        print(
            "names:      ",
            shop.root,
            "->",
            shop.name,
            "| recovered:",
            wc.document(shop.name) is shop,
            wc.reference(shop.root) is shop.ref(),
        )
        # [P1] Error policy: a not-ok object carries a serializable WebError;
        #      `ok` is the truth, is_ok()/is_empty() run even when not ok.
        assert missing.error is not None  # a not-ok document always carries one
        print(
            "not ok:     ",
            missing.error.type,
            "|",
            missing.message,
            "| is_ok:",
            missing.is_ok().get(),
            "| empty:",
            bool(missing.is_empty()),
        )

        # [M1] Selection: css or xpath, elements only; index/optional knobs.
        for card in shop.select_all(".card"):
            title = card.select(".title").text_content
            price = card.select("./span[@class='price']").text_content  # xpath
            link = card.select("a").attr("href")  # -> Reference
            # [M2] Follow the link: json selection uses a dotted path.
            item = link.resolve()
            print(
                f"card:        {title} {price} -> "
                f"{item.select('name').text_content} (stock {item.select('stock.count').text_content})"
            )

        # [render] One render(format) surface, dispatched to the backing for
        # the doc's kind. html gives markdown/text/elements/links/html; json
        # gives elements. Good cross-kind smoke test.
        page = shop
        print("title:      ", page.title)
        print("markdown:   ", page.markdown().splitlines()[0])
        print("text:       ", page.text(main_content_only=True)[:40])
        # skeleton(): a token-lean tag#id.class DOM outline -- an LLM reads this to
        # write CSS selectors for the page (see docs/llm-lazy-queries.md).
        print("skeleton:   ", page.skeleton(max_lines=3).replace("\n", " | "))
        print("elements:   ", [(e.type, e.text) for e in page.render("elements")][:3])
        print("links:      ", [r.path for r in page.render("links")])
        print("html:       ", page.render("html").strip()[:40])
        item = shop.select(".card a").attr("href").resolve()  # a json document
        print("json render:", [(e.type, e.text) for e in item.render("elements")][:3])

        # [M2] Events routed onto the document that caused them.
        print("doc events: ", [e.topic for e in shop.events])
        print("navigations:", [e.status_code for e in shop.events_of(NavigationEvent)])
        print("actions:    ", list(shop.action_events))  # empty until browser (M4)

        # [M2] Plugins: extend behaviour by registering a Backing. A registered
        #      backing is chosen before the built-ins, so this HtmlBacking subclass
        #      overrides the "markdown" render and super()s every other format.
        class Shouty(HtmlBacking):
            provides = frozenset({"render"})  # override render only; super()s the rest

            def render(self, core: Any, format: str, **options: Any) -> Any:
                if format == "markdown":
                    return core.dispatch("title").upper()
                return super().render(core, format, **options)

        wc.use(Shouty())
        print("plugin:     ", shop.render("markdown"))

        # [M3] Sessions: identity (cookies/headers) spanning fetches, with a
        #      ttl'd lifecycle. Cookies set by responses persist; sessions
        #      are isolated from each other.
        session = wc.session(ttl=300, headers={"x-app": "demo"})
        session.ref(f"{base}/login").resolve()
        print("session:    ", session.status, "cookies:", session.cookies)

        # [M4] browser=True -> a LiveDocument backed by a real page. Actions
        #      auto-wait and are recorded; the DOM/console/network are
        #      captured onto the document as events.
        live = wc.ref(f"{base}/app").resolve(browser=True)
        live.write("#qty", "3").click("#add")
        live.wait_for("#cart li", timeout=5.0)
        print("live dom:   ", live.select("#cart li").text_content)
        print("console:    ", [e.text for e in live.console])
        print("dom events: ", len(live.dom_mutations), "mutations captured")

        # [M4] LiveNode event narrowing: an element sees only its own subtree.
        cart = live.select("#cart")
        print("narrowed:   ", len(cart.events_of(DOMUpdateEvent)), "under #cart")

        # [P7] The reference carries the action chain, so re-resolving it
        #      (reload) reproduces the mutated state on a fresh page.
        print("chain:      ", [a["op"] for a in live.ref().actions])
        wc.release(live)  # page back to the pool

        # [probe] browser="probe": resolve BOTH tiers and compare -- the explicit
        #      "can I scrape this / what do I need" diagnostic. The /spa page injects
        #      its content via JS, so probe reports was_browser_required with the
        #      count of extra words the browser recovered.
        probed = wc.fetch(f"{base}/spa", browser="probe")
        pr = probed.summary().probe
        assert pr is not None  # probe mode always records the comparison
        print(
            "probe:      ",
            {"browser_required": pr.was_browser_required, "render_gain": pr.render_gain},
        )
        wc.release(probed)
        reloaded = live.reload()
        print("reloaded:   ", reloaded.select("#cart li", error=RETURN).ok)
        wc.release(reloaded)

        # [M2] Pool stats: bounded leases over http clients AND browser pages.
        print("pool:       ", wc.pool.stats())

    # [P2] One expression language: the same classes, recorded not executed.
    #      `doc`/`ref` are lazy roots; every op call appends a step to a typed
    #      Plan -- the wire form for the service. Reference(url) roots a plan.
    plan = (
        wq.reference(f"{base}/")
        .resolve()
        .select_all(".card")
        .extract(
            title=wq.doc.select(".title").text_content,
            price=wq.doc.select(".price").text_content,
            link=wq.doc.select("a").attr("href"),
        )
        .filter(wq.doc.field("price") != "")
        .project()
    )
    print("\nlazy plan:  ", plan._plan.describe()[:60], "...")
    print("wire form:  ", plan._plan.model_dump_json()[:70], "...")

    # [P3] One evaluator: the plan runs through the same @op implementations
    #      the eager calls use; a Collection fans out per element (bounded by
    #      the pool) and rows are delivered one at a time via stream=True.
    with WebClient() as wc:
        for row in plan.collect():
            print(f"  row:       {row['title']} {row['price']} -> {row['link']}")

        # [§8] Full-lazy trigger: .collect() runs a recorded plan directly, and
        #      wc.lazy is a lazy recorder bound to THIS client (companion to
        #      collect()). wc.lazy.ref(url) roots a client-bound plan.
        print("collect:    ", plan.collect()[0]["title"])
        bound = wc.lazy.ref(f"{base}/").resolve().select(".title").text_content
        print("wc.lazy:    ", bound.collect().get())

        # [§8] Polars-style free wq.when()/filter() on the lazy surface.

        labeled = (
            wq.ref.resolve()
            .select_all(".card")
            .extract(
                title=wq.doc.select(".title").text_content,
                tier=wq.when(wq.doc.select(".price").text_content != "")
                .then("priced")
                .otherwise("free"),
            )
            .project()
        )
        print(
            "free when:  ",
            [(r["title"], r["tier"]) for r in labeled.collect(wc.ref(f"{base}/"))],
        )
        priced = (
            wq.filter(
                wq.ref.resolve().select_all(".card"),
                wq.doc.select(".price").text_content != "",
            )
            .extract(title=wq.doc.select(".title").text_content)
            .project()
        )
        print("free filter:", [r["title"] for r in priced.collect(wc.ref(f"{base}/"))])

        # [P3] Follow each card's link (reference -> resolve) into its JSON
        #      detail; `when/then/otherwise` branches; a missing select is a
        #      not-ok field under the plan default, never an aborted plan.
        enriched = (
            wq.ref.resolve()
            .select_all(".card")
            .extract(
                title=wq.doc.select(".title").text_content,
                link=wq.doc.select("a.link").attr("href"),
                missing=wq.doc.select(".nope").text_content,
            )
            .extract(
                name=wq.doc.reference("link").resolve().select("name").attr("value"),
                stock=wq.doc.reference("link")
                .resolve()
                .select("stock.count")
                .attr("value"),
                tag=wq.when(wq.doc.field("title") == "Grinder")
                .then("bulky")
                .otherwise("small"),
            )
            .project()
        )
        print("streamed:")
        for row in enriched.stream(wc.ref(f"{base}/")):
            print(
                "  detail:   ",
                row["title"],
                "->",
                row["name"],
                row["stock"],
                row["tag"],
                "| missing:",
                row["missing"],
            )

        # [P3] Eager and lazy agree: the same extract on a resolved page.
        page = wc.ref(f"{base}/").resolve()
        cards = page.select_all(".card").extract(
            title=wq.doc.select(".title").text_content
        )
        print("eager:      ", cards.name, "->", [r["title"] for r in cards.project()])

        # [P6] Search is not a verb or a config type -- it is just an expression:
        #      resolve the query URL, pick the result nodes, extract a row each.
        #      summary() (a genuine digest) resolves a page to title + markdown.
        hits = (
            wc.ref(f"{base}/?q=coffee")
            .resolve()
            .select_all(".card")
            .limit(2)
            .extract(
                title=wq.doc.select(".title").text_content,
                url=wq.doc.select("a").attr("href"),
            )
            .project()
        )
        print("search:     ", [(h["title"], h["url"]) for h in hits])
        # summary(): a token-lean, deterministic overview -- facet sections
        # (transport / metadata / structure), keys-not-values.
        overview = wc.summary(f"{base}/")
        assert overview.transport and overview.metadata and overview.structure
        print(
            "summary:    ",
            {
                "ok": overview.transport.ok,
                "title": overview.metadata.title,
                "headings": len(overview.structure.toc),
                "cdn": overview.transport.cdn,
            },
        )
        # summary() also takes arbitrary backing methods by name (-> .extra),
        # so a crawl can decide exactly which backings populate each page.
        rich = wc.summary(f"{base}/", "transport", "title")
        print("summary+meth:", {"title": rich.extra.get("title")})

        # [D] Serialisable expressions: an LLM writes a lazy plan, encodes it to a
        #     short blob, and rebuilds + validates + pretty-prints it before running.
        expr = wq.doc.select(".title").text_content
        blob = expr.to_blob()
        print("expr blob:   ", blob)
        print("expr rebuilt:", from_blob(blob).explain())

        # [#3] Discover a site's real sitemap.xml URLs (none served here -> []).
        print("sitemaps:    ", [r.url for r in wc.discover_sitemaps(f"{base}/")])

    # [#7] The resiliency policy bundle is declared to a proxy service as request
    #      headers (the service is assumed to exist; here we just show the headers).
    from webclient.core.reference.models import ProxyPolicy, RatePolicy, Resolve
    from webclient.resiliency import policy_headers

    declared = policy_headers(
        Resolve(proxy=ProxyPolicy(pool="residential", geo="us"), rate=RatePolicy(rps=2))
    )
    print("policy hdrs: ", {k: declared[k] for k in sorted(declared)})

    # [async] The same eager surface, awaited. AsyncWebClient is the very same
    #      core with async dispatch (an instance flag, not a subclass): IO ops
    #      hand back an awaitable, so `await ac.ref(url).resolve()` chains async
    #      while in-memory ops stay synchronous. Deeper batching via `ac.lazy`.
    import asyncio

    from webclient import AsyncWebClient

    async def _async_demo() -> tuple:
        async with AsyncWebClient() as ac:
            document = await ac.fetch(f"{base}/")  # await at the IO boundary
            first = (await ac.ref(f"{base}/").resolve()).select(".title").text_content
            rows = await (
                wq.ref.resolve()
                .select_all(".card")
                .extract(title=wq.doc.select(".title").text_content)
                .project()
                .acollect(ac.ref(f"{base}/"))
            )
            return document.title, first, [r["title"] for r in rows]

    title, first_title, async_rows = asyncio.run(_async_demo())
    print("async fetch:", title, "| first:", first_title, "| async plan:", async_rows)

    # [M7] The same WebClient behind an HTTP API -- browser as a service.
    #      Every operation is one Plan submitted to /execute; a Document comes
    #      back as a handle ({"__doc__": meta}) and its content crosses the
    #      wire only via a further plan rooted at that handle's id.
    from fastapi.testclient import TestClient

    from webclient.service import create_app

    with TestClient(create_app(token="demo")) as api:
        auth = {"Authorization": "Bearer demo"}
        handle = api.post(
            "/execute",
            headers=auth,
            json={"plan": wq.ref.resolve()._plan.model_dump(), "url": f"{base}/"},
        ).json()["rows"]["__doc__"]
        print("\nservice fetch:", {k: handle[k] for k in ("kind", "ok")})
        did = handle["id"]
        md = api.post(
            "/execute",
            headers=auth,
            json={
                "plan": wq.doc.render("markdown")._plan.model_dump(),
                "document_id": did,
            },
        ).json()
        print("service render:", md["rows"].splitlines()[0])
        titles = api.post(
            "/execute",
            headers=auth,
            json={
                "plan": wq.doc.select_all(".title").text_content._plan.model_dump(),
                "document_id": did,
            },
        ).json()
        print("service select:", titles["rows"])
        plan = (
            wq.ref.resolve()
            .select_all(".card")
            .extract(title=wq.doc.select(".title").text_content)
            .project()
            ._plan
        )
        rows = api.post(
            "/execute",
            headers=auth,
            json={"plan": plan.model_dump(), "url": f"{base}/"},
        ).json()
        print("service plan:  ", rows["rows"])

    # [remote] Remote is just a different backend: the same WebClient over a
    #      RemoteWebClientCore, so execute runs server-side over HTTP with no
    #      local browser or lxml (httpx + pydantic only). A fetched document is
    #      a lazy handle; value ops run via rc.execute (one round trip each).
    import threading
    import time

    import uvicorn

    from webclient import RemoteWebClient

    app = create_app(token="demo")
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    )
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]

    with RemoteWebClient(f"http://127.0.0.1:{port}", token="demo") as rc:
        # The same eager surface over a remote core: rc.fetch(url) round-trips
        # once and returns a lightweight handle carrying its metadata (title/ok)
        # inline -- no content, no local lxml/browser.
        remote_doc = rc.fetch(f"{base}/")
        print("\nremote fetch:  ", remote_doc.title, "| ok:", remote_doc.ok)
        # Content ops are eager too -- each round-trips server-side and returns the
        # materialised value, exactly like the local client.
        print("remote render: ", remote_doc.render("markdown").splitlines()[0])
        # A multi-element fan-out is not per-element addressable server-side, so
        # batch it through .lazy: one recorded plan, one round-trip.
        print(
            "remote select: ",
            remote_doc.lazy.select_all(".title").text_content.collect(),
        )
        # identical plan API -- runs server-side, no local browser/lxml
        same_plan = (
            wq.ref.resolve()
            .select_all(".card")
            .extract(title=wq.doc.select(".title").text_content)
            .project()
        )
        print("remote plan:   ", same_plan.collect(rc.ref(f"{base}/")))
    server.should_exit = True
    app.state.wc.close()


if __name__ == "__main__":
    main()
