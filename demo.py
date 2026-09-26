"""demo.py -- one clean tour of every implemented webclient feature.

The tour reads as a sequence of USER STORIES (one function each): build a request,
crawl a site, select + render, record a browser journey into a replayable Plan,
drive agent loops, paginate, extract lazily, and run the same plans async / over an
HTTP service / on a remote client. ``roadmap_tour`` then covers the 2026-09 roadmap
(the ledger, traces + replay, tools, scripts + rrweb, loops, patterns, the UI)
against the shared lab, and leaves ``traces/demo`` for ``make serve`` -> /ui.

The through-line is "everything is a Plan": :func:`browser_journey` RECORDS an eager
session into one portable Plan and replays it, and the whole back half runs recorded
Plans through the one evaluator -- eager, lazy, async, service and remote all agree.

Runs fully offline: it serves its own demo site on localhost (needs chromium).

    env/bin/python demo.py
"""

from __future__ import annotations

import http.server
import threading
from typing import Any

from webclient import (
    RETURN,
    DOMUpdateEvent,
    HtmlBacking,
    NavigationEvent,
    WaitConfig,
    WaitEvent,
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
  <div class="card"><h2 class="title">Kettle</h2>
    <a class="link" href="/items/3">view</a><span class="price">$79</span></div>
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
# a page that hides its records in shadow DOM + a same-origin iframe -- invisible to a
# plain HTML snapshot; the render inlines both so the content (and skeleton) captures them.
SHADOW = b"""
<html><head><title>Shadow</title></head><body><main>
  <div id="host"></div>
  <iframe src="/frame"></iframe>
  <script>
    const r = document.getElementById('host').attachShadow({mode:'open'});
    r.innerHTML = '<ul><li class="rec">shadow record A</li>'
                + '<li class="rec">shadow record B</li></ul>';
  </script>
</main></body></html>
"""
FRAME = b'<html><body><p class="frec">iframe record</p></body></html>'
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


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/" or self.path.startswith("/?"):  # html (query ignored)
            body, ctype = PAGE, "text/html; charset=utf-8"
        elif self.path == "/items/1":  # json documents
            body, ctype = ITEM % (1, b"Aeropress"), "application/json"
        elif self.path == "/items/2":
            body, ctype = ITEM % (2, b"Grinder"), "application/json"
        elif self.path == "/items/3":
            body, ctype = ITEM % (3, b"Kettle"), "application/json"
        elif self.path.startswith("/feed"):  # paginated + cookie-aware
            from urllib.parse import parse_qs, urlparse

            page = int(parse_qs(urlparse(self.path).query).get("p", ["1"])[0])
            user = "friend" if "token=tok" in (self.headers.get("Cookie") or "") else "guest"
            nxt = f'<a class="next" href="/feed?p={page + 1}">more</a>' if page < 3 else ""
            body = f"<html><body><h1>feed p{page} for {user}</h1>{nxt}</body></html>".encode()
            ctype = "text/html"
        elif self.path.startswith("/releases"):  # a paginated dataset (rel=next)
            from urllib.parse import parse_qs, urlparse

            page = int(parse_qs(urlparse(self.path).query).get("p", ["1"])[0])
            recs = "".join(f'<article class="rel">v{page}.{i}</article>' for i in range(2))
            nxt = f'<link rel="next" href="/releases?p={page + 1}">' if page < 3 else ""
            body = f"<html><head>{nxt}</head><body><main>{recs}</main></body></html>".encode()
            ctype = "text/html"
        elif self.path == "/login":  # sets a session cookie
            self.send_response(200)
            self.send_header("Set-Cookie", "token=tok; Path=/")
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"welcome")
            return
        elif self.path == "/spa":  # a JS-gated page (content injected by script)
            body, ctype = SPA, "text/html"
        elif self.path == "/app":  # a JS-driven live page
            body, ctype = APP, "text/html"
        elif self.path == "/shadow":  # records hidden in shadow DOM + a same-origin iframe
            body, ctype = SHADOW, "text/html"
        elif self.path == "/frame":  # the iframe's content
            body, ctype = FRAME, "text/html"
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


# -- output helpers: the tour reads as titled sections of aligned key/value lines ----

def _section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(3, 64 - len(title)))


def _show(label: str, *values: Any) -> None:
    print(f"  {label + ':':<15}", *values)


# -- the stories -----------------------------------------------------------------------

def references(base: str) -> None:
    """A Reference is a pure request SPEC: build one, derive variants, inspect -- no IO."""
    _section("references (pure request specs)")
    spec = from_url(f"{base}/?utm=x", params={"page": "1"})
    _show("url", spec.url)
    _show("derived", spec.with_params(page="2").replace(fragment="top").url)
    _show("joined", spec.join("items/1").url)


def fetch_and_crawl(wc: WebClient, base: str) -> tuple[Any, Any]:
    """Fetch through a redirect (loud by default, ``optional`` lenient), then crawl: a
    client-held, scoped traversal -- steer a round (``step``) or self-drive (``run``), with
    a custom driver an option. Returns ``(shop, missing)`` for the addressing story."""
    _section("fetch + crawl")
    shop = wc.ref(f"{base}/old").resolve()
    _show("final url", shop.final_url)
    missing = wc.ref(f"{base}/nope").resolve(error=RETURN)
    _show("optional", missing.status_code, "ok:", missing.ok)

    with wc.crawl(f"{base}/feed", auto=True, max_pages=4, browser=False) as crawl:
        crawl.step()  # one round: fetch the seed, discover + score its edges
        _show("frontier", [(round(e.score, 2), e.url.replace(base, "")) for e in crawl.frontier])
        crawl.run()   # then self-drive the rest
        _show("crawled", [(p.final_url or p.url).replace(base, "") for p in crawl.pages])

    from webclient.core.crawl import from_picks  # a custom driver: follow only /feed* pages

    picker = from_picks(lambda edges: [e.url for e in edges if "/feed" in e.url])
    with wc.crawl(f"{base}/feed", max_pages=4, browser=False, driver=picker) as steered:
        steered.run()
        _show("driven", [(p.final_url or p.url).replace(base, "") for p in steered.pages])
    _show("sitemap", [r.url.replace(base, "") for r in wc.sitemap(f"{base}/")])
    return shop, missing


def addressing_and_errors(wc: WebClient, shop: Any, missing: Any) -> None:
    """Every object is addressable (short scoped names, a ref->doc root chain, recovery by
    name); a not-ok object carries a serializable WebError while ``is_ok``/``is_empty`` still run."""
    _section("addressing + error policy")
    _show("names", shop.root, "->", shop.name, "| recovered:",
          wc.document(shop.name) is shop, wc.reference(shop.root) is shop.ref())
    assert missing.error is not None
    _show("not ok", missing.error.type, "|", missing.message,
          "| is_ok:", missing.is_ok(), "| empty:", bool(missing.is_empty()))


def selection_and_render(wc: WebClient, shop: Any) -> None:
    """Selection (css OR xpath, elements only), following a link into its JSON detail, and the
    one ``render(format)`` surface (markdown / text / skeleton / controls / elements / links)."""
    _section("selection + render")
    for card in shop.select_all(".card"):
        title = card.select(".title").attr("text")
        price = card.select("./span[@class='price']").attr("text")  # xpath
        item = card.select("a").attr("href").resolve()  # href -> Reference -> json doc
        _show("card", f"{title} {price} -> {item.select('name').attr('text')}"
                      f" (stock {item.select('stock.count').attr('text')})")

    _show("title", shop.title)
    _show("markdown", shop.markdown().splitlines()[0])
    _show("text", shop.text(main_content_only=True)[:40])
    _show("skeleton", shop.skeleton(max_lines=3).replace("\n", " | "))  # an LLM reads this
    _show("controls", [(c.index, c.role, c.selector) for c in shop.controls()][:3])
    _show("elem table", shop.element_table().replace("\n", " | ")[:64])
    _show("elements", [(e.type, e.text) for e in shop.render("elements")][:3])
    _show("links", [r.path for r in shop.render("links")])
    item = shop.select(".card a").attr("href").resolve()
    _show("json render", [(e.type, e.text) for e in item.render("elements")][:3])

    _show("doc events", [e.topic for e in shop.events])
    _show("navigations", [e.status_code for e in shop.events_of(NavigationEvent)])

    class Shouty(HtmlBacking):  # a plugin Backing: override ONE format, super() the rest
        provides = frozenset({"render"})

        def render(self, core: Any, format: str, **options: Any) -> Any:
            return core.dispatch("title").upper() if format == "markdown" else super().render(core, format, **options)

    wc.use(Shouty())
    _show("plugin", shop.render("markdown"))


def sessions(wc: WebClient, base: str) -> None:
    """A session is identity (cookies/headers) with a ttl'd lifecycle, isolated per session;
    response cookies persist across its fetches."""
    _section("sessions")
    session = wc.session(ttl=300, headers={"x-app": "demo"})
    session.ref(f"{base}/login").resolve()
    _show("session", session.status, "cookies:", session.cookies)


def browser_journey(wc: WebClient, base: str) -> Any:
    """Everything is a Plan: RECORD an eager browser session -- resolve + interactions -- into
    ONE portable Plan (secrets scrubbed), then REPLAY that Plan on a fresh page to reproduce the
    reached state. The live page captures DOM/console/network as events. Returns the live doc."""
    _section("browser + recorder (a journey as a Plan)")
    with wc.record() as rec:
        live = rec.ref(f"{base}/app").resolve(browser=True)
        live.write("#qty", "3").click("#add").wait_for("#cart li", timeout=5.0)
        _show("live dom", live.select("#cart li").attr("text"))
        _show("console", [e.text for e in live.console])
        _show("dom events", len(live.dom_mutations), "mutations captured")
        cart = live.select("#cart")  # a LiveNode sees only its own subtree's events
        _show("narrowed", len(cart.events_of(DOMUpdateEvent)), "under #cart")
        recorded = rec.plan  # the resolve + ordered interactions, as a Plan
    wc.release(live)

    _show("recorded", recorded.describe())
    replayed = recorded.collect()  # replay = run the Plan on a fresh page
    _show("replayed", replayed.select("#cart li").attr("text"))
    wc.release(replayed)
    return live


def agent_loops(wc: WebClient, base: str, shop: Any) -> None:
    """Two bounded, page-scoped loops driven by a typed policy (a plain fn here; an LLM adapter
    in production): ``drive`` acts on a held page (returning typed actions, recorded as a Plan),
    and ``build_query`` picks a RECORD + FIELDS by index and assembles a durable extract query --
    the model never authors a selector."""
    _section("agent loops (interact + query)")
    from webclient.llm import Done, Observation, Type, WaitFor, drive

    def policy(obs: Observation) -> Any:  # scripted: add 2 to the cart, then finish
        if obs.step == 0:
            qty = next((e for e in obs.elements if e.role == "textbox"), None)
            return Type(index=qty.index, text="2") if qty else Type(selector="#qty", text="2")
        if obs.step == 1:
            return WaitFor(selector="#cart")
        return Done(result="added to cart")

    with wc.record() as rec2:
        page = rec2.ref(f"{base}/app").resolve(browser=True)
        page.click("#add")  # seed the cart, then hand off to the loop
        run = drive(page, policy, max_steps=6)
        journey = rec2.plan
    wc.release(page)
    _show("agent", run.reason, f"in {run.steps} step(s) — {run.result!r}")
    _show("journey", journey.describe())

    from webclient.llm import QueryDecision, QueryObservation, build_query

    def qpolicy(obs: QueryObservation) -> Any:
        if obs.step == 0 and obs.records:  # the top repeated record region
            return QueryDecision(record=obs.records[0].index)
        if obs.fields:  # add name + price columns from the record's leaves
            cols = {"name": obs.fields[0].index}
            price = next((f for f in obs.fields if f.name.startswith("$")), None)
            if price:
                cols["price"] = price.index
            return QueryDecision(fields=cols, done=True)
        return QueryDecision(done=True)

    qrun = build_query(shop, qpolicy)
    _show("query agent", qrun.describe[:60])
    _show("query rows", qrun.row_count, qrun.sample[:2])


def pagination(wc: WebClient, base: str) -> None:
    """Walk a paginated dataset into a Collection of same-structure pages (the body then extracts
    across ALL pages, not page one), and the manual/agent twin ``wc.paginate`` -- a stateful walk
    on a BoundedLoop with ``step``/``run`` and a precise stop ``verdict``."""
    _section("pagination (bound op + session)")
    first = wc.fetch(f"{base}/releases?p=1")
    _show("pager hint", first.pagination().value.best.code if first.pagination().present else "none")
    pages = first.paginate(next=wq.doc.next_link(), max_pages=5)
    _show("paginate", len(list(pages)), "pages walked")
    dataset = (
        wq.reference(f"{base}/releases?p=1").resolve()
        .paginate(next=wq.doc.next_link(), max_pages=5)
        .select_all("article.rel").extract(v=wq.doc.attr("text")).project()
    ).collect()
    _show("dataset", [r["v"] for r in dataset])

    with wc.paginate(f"{base}/releases?p=1", next=wq.doc.next_link(), max_pages=5) as pg:
        pg.step()
        v = pg.run().verdict
        assert v is not None
        _show("paginate pg", v.pages, "pages, stop:", v.stop)


def flags_and_deep_dom(wc: WebClient, base: str) -> None:
    """``browser="auto"`` escalates a JS-gated page on its detected flags (the spa flag, built from
    static + rendered signals). Records hidden in shadow DOM / a same-origin iframe are inlined by
    the render, so the captured content + the shadow_dom / iframe flags hold them."""
    _section("flags + shadow/iframe")
    probed = wc.fetch(f"{base}/spa", browser="auto")
    spa = probed.spa()
    _show("flags", {"spa": spa.present, "confidence": spa.confidence,
                    "evidence": [s.name for s in spa.signals], "tiers": probed.transport().escalation})

    deep = wc.fetch(f"{base}/shadow", browser="always")
    deep_text = deep.attr("text") or ""
    _show("shadow/iframe", {"shadow_dom": (deep.shadow_dom().present, deep.shadow_dom().value),
                            "iframe": (deep.iframe().present, deep.iframe().value),
                            "shadow_inlined": "shadow record A" in deep_text,
                            "iframe_inlined": "iframe record" in deep_text})
    wc.release(deep)


def sequences_and_waits(wc: WebClient, base: str, live: Any) -> None:
    """A multi-step script against ONE held page as a SINGLE plan (``.step(action)`` chains
    interactions, interleaved ``.extract`` captures accumulate), a controllable wait strategy, a
    browser render carrying the REAL Playwright response, reload, and the pool's bounded leases."""
    _section("sequences + waits + pool")
    seq = (
        wq.ref.resolve(browser="always")
        .step(wq.doc.write("#qty", "7")).step(wq.doc.click("#add")).step(wq.doc.wait_for("#cart li"))
        .extract(first=wq.doc.select("#cart li").attr("text"))
        .step(wq.doc.write("#qty", "9")).step(wq.doc.click("#add"))
        .extract(second=wq.doc.select("#cart li", index=1).attr("text"))
        .project()
    )
    _show("sequence", wc.execute(seq, wc.ref(f"{base}/app")))

    rendered = wc.fetch(f"{base}/app", browser=True)
    _show("browser xport", {"status": rendered.transport().status_code,
                            "header_keys": len(rendered.transport().header_keys),
                            "final_url": (rendered.final_url or "").endswith("/app")})
    wc.release(rendered)

    waited = wc.fetch(f"{base}/app", browser=True,
                      wait=WaitConfig(event=WaitEvent.SELECTOR, selector="#add", timeout=5.0))
    _show("waited", waited.select("#add", error=RETURN).ok)
    wc.release(waited)

    reloaded = live.reload()
    _show("reloaded", reloaded.select("#cart li", error=RETURN).ok)
    wc.release(reloaded)
    _show("pool", wc.pool.stats())


def lazy_plans(base: str) -> Any:
    """One expression language: ``doc``/``ref`` are lazy roots; each op appends a step to a typed,
    wire-safe Plan. ``explain`` renders a SQL-EXPLAIN step tree, ``wireframe`` an HTML picture.
    Returns the plan for the evaluator story."""
    _section("lazy plans (record, don't execute)")
    plan = (
        wq.reference(f"{base}/").resolve().select_all(".card")
        .extract(title=wq.doc.select(".title").attr("text"),
                 price=wq.doc.select(".price").attr("text"),
                 link=wq.doc.select("a").attr("href"))
        .filter(wq.doc.field("price") != "")
        .project()
    )
    _show("lazy plan", plan._plan.describe()[:60], "...")
    _show("wire form", plan._plan.model_dump_json()[:70], "...")
    print("  explain:")
    for line in plan.explain().splitlines():
        print("     ", line)
    _show("wireframe", f"{len(plan.wireframe())} bytes of self-contained HTML")
    return plan


def evaluator(base: str, plan: Any) -> None:
    """One evaluator runs the recorded Plan through the same @op implementations the eager calls
    use: fan-out + streaming, free ``when``/``filter``, reference-following enrichment, the
    eager==lazy identity, search-as-an-expression, the facet ops, and blob round-trip."""
    _section("evaluator (plans run, eager == lazy)")
    with WebClient() as wc:
        for row in plan.collect():
            _show("row", f"{row['title']} {row['price']} -> {row['link']}")
        _show("collect", plan.collect()[0]["title"])
        bound = wc.lazy.ref(f"{base}/").resolve().select(".title").attr("text")
        _show("wc.lazy", bound.collect().get())

        labeled = (
            wq.ref.resolve().select_all(".card")
            .extract(title=wq.doc.select(".title").attr("text"),
                     tier=wq.when(wq.doc.select(".price").attr("text") != "").then("priced").otherwise("free"))
            .project()
        )
        _show("free when", [(r["title"], r["tier"]) for r in labeled.collect(wc.ref(f"{base}/"))])
        priced = (
            wq.filter(wq.ref.resolve().select_all(".card"), wq.doc.select(".price").attr("text") != "")
            .extract(title=wq.doc.select(".title").attr("text")).project()
        )
        _show("free filter", [r["title"] for r in priced.collect(wc.ref(f"{base}/"))])

        enriched = (
            wq.ref.resolve().select_all(".card")
            .extract(title=wq.doc.select(".title").attr("text"),
                     link=wq.doc.select("a.link").attr("href"),
                     missing=wq.doc.select(".nope", error=RETURN).attr("text"))
            .extract(name=wq.doc.reference("link").resolve().select("name").attr("value"),
                     stock=wq.doc.reference("link").resolve().select("stock.count").attr("value"),
                     tag=wq.when(wq.doc.field("title") == "Grinder").then("bulky").otherwise("small"))
            .project()
        )
        print("  streamed:")
        for row in enriched.stream(wc.ref(f"{base}/")):
            _show("  detail", row["title"], "->", row["name"], row["stock"], row["tag"], "| missing:", row["missing"])

        page = wc.ref(f"{base}/").resolve()
        cards = page.select_all(".card").extract(title=wq.doc.select(".title").attr("text"))
        _show("eager", cards.name, "->", [r["title"] for r in cards.project()])
        overview = page.extract(title=wq.doc.select(".title").attr("text"),
                                link=wq.doc.select(".card a").attr("href")).project()
        _show("doc row", overview)

        hits = (
            wc.ref(f"{base}/?q=coffee").resolve().select_all(".card").limit(2)
            .extract(title=wq.doc.select(".title").attr("text"), url=wq.doc.select("a").attr("href")).project()
        )
        _show("search", [(h["title"], h["url"]) for h in hits])
        page = wc.fetch(f"{base}/")
        _show("facets", {"ok": page.transport().ok, "title": page.metadata().title,
                         "headings": len(page.structure().toc), "cdn": page.transport().cdn})

        expr = wq.doc.select(".title").attr("text")  # an LLM writes a plan -> a short blob -> back
        blob = expr.to_blob()
        _show("expr blob", blob)
        _show("expr rebuilt", from_blob(blob).describe())
        _show("sitemaps", [r.url for r in wc.sitemap(f"{base}/")])


def policy_headers_story(base: str) -> None:
    """The resiliency policy bundle (proxy / rate / ...) declares itself to a proxy service as
    request headers -- shown here, the service assumed to exist."""
    _section("policy headers")
    from webclient.policy import ProxyPolicy, RatePolicy, Resolve, policy_headers

    declared = policy_headers(Resolve(proxy=ProxyPolicy(pool="residential", geo="us"), rate=RatePolicy(rps=2)))
    _show("policy hdrs", {k: declared[k] for k in sorted(declared)})


def async_story(base: str) -> None:
    """The same eager surface, awaited: AsyncWebClient is the very same core with async dispatch
    (an instance flag, not a subclass) -- IO ops hand back an awaitable, in-memory ops stay sync."""
    _section("async (the same core, awaited)")
    import asyncio

    from webclient import AsyncWebClient

    async def _run() -> tuple[Any, Any, Any]:
        async with AsyncWebClient() as ac:
            document = await ac.fetch(f"{base}/")
            first = (await ac.ref(f"{base}/").resolve()).select(".title").attr("text")
            rows = await (
                wq.ref.resolve().select_all(".card")
                .extract(title=wq.doc.select(".title").attr("text")).project()
                .acollect(ac.ref(f"{base}/"))
            )
            return document.title, first, [r["title"] for r in rows]

    title, first_title, async_rows = asyncio.run(_run())
    _show("async fetch", title, "| first:", first_title, "| async plan:", async_rows)


def service_story(base: str) -> None:
    """The same WebClient behind an HTTP API -- browser as a service. Every operation is one Plan
    submitted to ``/execute``; a Document rides back as a handle, its content only via a further
    plan rooted at that handle's id."""
    _section("service (browser as an HTTP API)")
    from fastapi.testclient import TestClient

    from webclient.service import create_app

    with TestClient(create_app(token="demo")) as api:
        auth = {"Authorization": "Bearer demo"}
        handle = api.post("/execute", headers=auth,
                          json={"plan": wq.ref.resolve()._plan.model_dump(), "url": f"{base}/"}).json()["rows"]["__doc__"]
        _show("service fetch", {k: handle[k] for k in ("kind", "ok")})
        did = handle["id"]
        md = api.post("/execute", headers=auth,
                      json={"plan": wq.doc.render("markdown")._plan.model_dump(), "document_id": did}).json()
        _show("service render", md["rows"].splitlines()[0])
        titles = api.post("/execute", headers=auth,
                          json={"plan": wq.doc.select_all(".title").attr("text")._plan.model_dump(), "document_id": did}).json()
        _show("service select", titles["rows"])
        plan = (
            wq.ref.resolve().select_all(".card")
            .extract(title=wq.doc.select(".title").attr("text")).project()._plan
        )
        rows = api.post("/execute", headers=auth, json={"plan": plan.model_dump(), "url": f"{base}/"}).json()
        _show("service plan", rows["rows"])


def remote_story(base: str) -> None:
    """Remote is just a dispatch mode: the same WebClient in "remote" mode over an HTTP connection,
    so execute runs server-side (httpx + pydantic, no local browser/lxml). A fetched document is a
    lazy handle; content ops round-trip; a remote crawl runs as one plan server-side."""
    _section("remote (dispatch mode, server-side)")
    import threading
    import time

    import uvicorn

    from webclient import RemoteWebClient
    from webclient.service import create_app

    app = create_app(token="demo")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]

    with RemoteWebClient(f"http://127.0.0.1:{port}", token="demo") as rc:
        remote_doc = rc.fetch(f"{base}/")  # one round-trip -> a metadata handle
        _show("remote fetch", remote_doc.title, "| ok:", remote_doc.ok)
        _show("remote render", remote_doc.render("markdown").splitlines()[0])
        _show("remote select", remote_doc.lazy.select_all(".title").attr("text").collect())  # batch via .lazy
        same_plan = (
            wq.ref.resolve().select_all(".card")
            .extract(title=wq.doc.select(".title").attr("text")).project()
        )
        _show("remote plan", same_plan.collect(rc.ref(f"{base}/")))
        remote_crawl = rc.crawl(f"{base}/feed", auto=True, max_pages=3, browser=False, obey_robots=False)
        remote_crawl.run()
        _show("remote crawl", [(p.final_url or p.url).replace(base, "") for p in remote_crawl.pages])
    server.should_exit = True
    app.state.wc.close()


def main() -> None:
    base = serve()
    print("webclient demo — a tour of every feature, fully offline\n" + "=" * 62)

    references(base)
    with WebClient(default_headers={"user-agent": "webclient-demo"}) as wc:
        wc.bus.subscribe("network", lambda e: None)  # everything observable crosses one bus
        shop, missing = fetch_and_crawl(wc, base)
        addressing_and_errors(wc, shop, missing)
        selection_and_render(wc, shop)
        sessions(wc, base)
        live = browser_journey(wc, base)
        agent_loops(wc, base, shop)
        pagination(wc, base)
        flags_and_deep_dom(wc, base)
        sequences_and_waits(wc, base, live)

    plan = lazy_plans(base)
    evaluator(base, plan)
    policy_headers_story(base)
    async_story(base)
    service_story(base)
    remote_story(base)
    roadmap_tour()


def roadmap_tour() -> None:
    """The 2026-09 roadmap features (Phases 0-7): the error ledger, traces + the three replay
    modes, the tool registry, named scripts + rrweb, loops with drivers / checkpoints, pattern
    hints, and the UI -- all against the shared lab (``webclient.lab``). Writes ``traces/demo.jsonl``
    for ``make serve`` + the separate UI (webclient-ui)."""
    from pathlib import Path

    from webclient import Ask, Script, ScriptEvent
    from webclient.lab import serve as serve_lab
    from webclient.replay import Replay
    from webclient.tools import TOOLS, dispatch
    from webclient.trace import read

    lab = serve_lab()
    trace_dir = Path("traces") / "demo.jsonl"  # a trace is ONE file: the whole run, replayable every way
    trace_dir.unlink(missing_ok=True)
    _section(f"roadmap tour (lab at {lab}/lab )")

    with WebClient(timeout=15.0) as wc, wc.trace(trace_dir) as tr:
        # the Plan the trace carries (Run replays the trace against it: its pipeline graph and stages)
        tr.plan = (wq.reference(f"{lab}/lab/shop").resolve().select_all("div.card")
                   .extract(title=wq.doc.select(".title").attr("text"), price=wq.doc.select(".price").attr("text"),
                            link=wq.doc.select("a").attr("href")).project())
        # ...and RUN it, in the trace: every event it causes is attached to its step (Run replays it as the plan)
        _show("the plan", [r["title"] for r in wc.execute(tr.plan)])
        # Errors are catalogued + bound, and NOTHING disappears: a RETURN-policy miss still lands
        # on the ledger (doc.errors / wc.errors) as an ErrorEvent in the trace.
        shop = wc.fetch(f"{lab}/lab/shop")
        shop.select(".nope", error=RETURN)
        err = shop.errors[0]
        _show("ledger", err.code, "| remedy:", err.remedy, "| op:", err.op, "| raised:", wc.errors[-1].raised)

        # Pattern hints (Signals/Flags): the repeating record list to select_all, without an LLM.
        hint = shop.patterns(for_="extract")[0]
        _show("pattern", hint.name, hint.subject, f"x{hint.count}", "conf", hint.confidence)

        # The tool registry: one declaration -> Python / MCP / POST /tools/{name}.
        card = dispatch("card", {"url": f"{lab}/lab/shop"}, wc)
        _show("tools", len(TOOLS), "registered | card:", card["title"], card["flags"], card["final_tier"])

        # Loops: locate = a crawl with a goal; a resolve driver may ASK a human (checkpoint/resume).
        found = wc.locate(f"{lab}/lab/shop", until=lambda c: (c.title or "").startswith("About"),
                          browser=False, obey_robots=False, max_pages=6, width=2)
        _show("locate", found.reason, [p.title for p in found.found], "after", found.rounds, "round(s)")
        wc.driver("resolve", lambda obs: Ask(reason="render?", options=["browser"]) if "spa" in obs.present else None)
        spa = wc.fetch(f"{lab}/lab/spa", browser="auto")
        _show("resolve ask", spa.pending.reason if spa.pending else None, "| tier:", spa.transport().final_tier)
        rendered = wc.escalate(spa, "browser")  # the human's answer: one hop, by hand
        _show("escalated", rendered.transport().escalation, "| records:", len(rendered.select_all("li.item")))
        wc.release(rendered)
        wc.driver("resolve", None)

        # Named, phased, togglable scripts (+ rrweb recording, on because we are tracing).
        wc.scripts.register(Script("demo.title", "() => document.title", on="load"))
        live = wc.ref(f"{lab}/lab/app").resolve(browser=True).collect()
        live.write("#qty", "2").click("#add").wait_for("#cart li")
        ran = [s.script for s in wc.bus.since(0, topic="script")
               if isinstance(s, ScriptEvent) and s.script == "demo.title"]
        _show("scripts", [s.name for s in wc.scripts.list()][:4], "... | demo.title ran:", bool(ran))
        wc.release(live)

    # Replay, three ways -- offline projections, a HAR, or the live plan.
    reader = read(trace_dir)
    _show("trace", reader.count, "events |", len(reader.snapshots), "snapshots |",
          len(reader.rrweb()), "rrweb events |", len(reader.har()["log"]["entries"]), "HAR entries")
    with Replay(trace_dir) as rep:
        offline = rep.document(shop.name)
        assert offline is not None
        _show("static replay", [c.select(".title").attr("text") for c in offline.select_all(hint.subject)],
              "| same skeleton:", offline.skeleton() == shop.skeleton())
        cart = rep.document(live.name)
        assert cart is not None
        _show("last snapshot", [li.attr("text") for li in cart.select_all("#cart li")])
        har = rep.har_path
    with WebClient(har=str(har)) as offline_wc:
        again = offline_wc.fetch(f"{lab}/lab/shop")
        miss = offline_wc.fetch(f"{lab}/lab/never", optional=True)
        _show("har replay", again.title, "| unrecorded ->", miss.error.code if miss.error else None)

    record_onboarding(lab)
    print("\nUI: run `make serve` then open http://localhost:8000/ui/  (trace 'demo' is listed)")


def record_onboarding(lab: str) -> None:
    """[R] Record an onboarding run as ONE trace (``traces/onboarding.jsonl``) -- the website's
    onboarding page REPLAYS it (no model on the public site: the scripted demo model, badged).
    The pipeline pauses at the confirm gate (``interactive=True``) and is resumed with "yes",
    so the gate, the resume and every stage boundary are in the stream."""
    import json as _json
    from pathlib import Path

    from webclient.pipelines import Brief, SearchHit, onboard_company

    shop = f"{lab}/lab/shop"
    code = ('wq.doc.select_all("div.card").extract(title=wq.doc.select(".title").attr("text"), '
            'price=wq.doc.select(".price").attr("text")).project()')

    def search(query: str, k: int) -> list:
        return [SearchHit(url=shop, title="Roasters", snippet="the featured products")]

    def llm(prompt: str) -> str:  # the demo model: scripted by prompt, deterministic, $0
        if "frontier links" in prompt:
            return "[]"
        if "crawled pages" in prompt:
            return _json.dumps([{"url": shop, "kind": "page", "tier": "must", "note": "the product grid"}])
        if "Assess this page" in prompt:
            return _json.dumps({"dataset_present": True, "is_queryable": True, "completeness": "full",
                                "has_pagination": False, "scrapability": 9, "verdict": "a full product list"})
        if "query code" in prompt or "write a query" in prompt:
            return f"here is the query:\n{code}"
        return "{}"

    path = Path("traces") / "onboarding.jsonl"
    with WebClient(timeout=15.0) as wc, wc.trace(path) as tr:
        result = onboard_company(
            "Roasters", Brief(description="the featured products with their prices", fields=["title", "price"], search="products"),
            wc=wc, llm=llm, search=search, browser=False, interactive=True,
        )
        if result.pending is not None:  # the confirm gate: a human says yes, once
            result = result.resume("yes")
        if result.query is not None:  # the trace carries the plan it made (Run replays the query as that plan)
            tr.plan = result.query.blob
    print("onboarding:    ", "ok" if result.ok else result.reason, "| trace:", path)


if __name__ == "__main__":
    main()
