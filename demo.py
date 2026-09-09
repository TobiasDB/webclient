"""demo.py -- one clean tour of every implemented webclient feature.

Maintained with every milestone. Sections marked [M<n>] appear as their
milestone lands; the interface spec is /models.py, the roadmap PLAN.md.

Runs fully offline: it serves its own demo site on localhost.

    env/bin/python demo.py
"""
from __future__ import annotations

import http.server
import threading

from webclient import (
    DOMUpdateEvent,
    NavigationEvent,
    Reference,
    Renderer,
    WebClient,
    q,
)
from webclient.models import Document

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
        if self.path == "/":                       # html page
            body, ctype = PAGE, "text/html; charset=utf-8"
        elif self.path == "/items/1":              # json documents
            body, ctype = ITEM % (1, b"Aeropress"), "application/json"
        elif self.path == "/items/2":
            body, ctype = ITEM % (2, b"Grinder"), "application/json"
        elif self.path.startswith("/feed"):        # paginated + cookie-aware
            from urllib.parse import parse_qs, urlparse
            page = int(parse_qs(urlparse(self.path).query).get("p", ["1"])[0])
            user = "friend" if "token=tok" in (self.headers.get("Cookie") or "") else "guest"
            nxt = f'<a class="next" href="/feed?p={page + 1}">more</a>' if page < 3 else ""
            body = (f"<html><body><h1>feed p{page} for {user}</h1>{nxt}"
                    "</body></html>").encode()
            ctype = "text/html"
        elif self.path == "/login":                # sets a session cookie
            self.send_response(200)
            self.send_header("Set-Cookie", "token=tok; Path=/")
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"welcome")
            return
        elif self.path == "/app":                  # a JS-driven live page
            body, ctype = APP, "text/html"
        elif self.path == "/old":                  # a redirect hop
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

    def log_message(self, *args):  # quiet
        pass


def serve() -> str:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}"


def main() -> None:
    base = serve()

    # [M1] References are pure request specs -- build, derive, inspect.
    ref = Reference.from_url(f"{base}/?utm=x", params={"page": "1"})
    print("url:        ", ref.url)
    print("derived:    ", ref.with_params(page="2").replace(fragment="top").url)
    print("joined:     ", ref.join("items/1").url)

    # [M2] A WebClient owns the pool, bus, plugins; it is the lifecycle root.
    with WebClient(default_headers={"user-agent": "webclient-demo"}) as wc:

        # [M2] Live event stream: everything observable crosses one bus.
        wc.bus.subscribe("network", lambda e: print(
            f"event:       {e.topic} #{e.seq} {e.status_code} {e.request.path}"))

        # [M2] Fetch through a redirect; loud by default, optional=True lenient.
        doc = wc.ref(f"{base}/old").fetch()
        print("final url:  ", doc.final_url)
        missing = wc.ref(f"{base}/nope").fetch(optional=True)
        print("optional:   ", missing.status_code, "ok:", missing.ok)

        # [M1] Selection: css or xpath, elements only; index/optional knobs.
        for card in doc.select_all(".card"):
            title = card.select(".title").text
            price = card.select("./span[@class='price']").text     # xpath
            link = card.select("a").attr("href")                   # -> Reference
            # [M2] Follow the link: json documents get typed access + query.
            item = link.fetch()
            print(f"card:        {title} {price} -> "
                  f"{item.json.data['name']} (stock {item.json.query('stock.count')})")

        # [M1] Typed views + [M2] plugin-backed representations.
        page = doc.html
        print("title:      ", page.title)
        print("markdown:   ", page.markdown.splitlines()[0])
        print("text:       ", page.render("text", main_content_only=True)[:40])
        print("elements:   ", [(e.type, e.text) for e in page.elements][:3])
        print("links:      ", [r.path for r in page.links()])

        # [M2] Events routed onto the document that caused them.
        print("doc events: ", [e.topic for e in doc.events])
        print("navigations:", [e.status_code for e in doc.events_of(NavigationEvent)])
        print("actions:    ", list(doc.actions))   # empty until browser (M4)

        # [M2] Plugins: replace a core renderer by registration alone.
        class Shouty(Renderer):
            name: str = "shouty"
            kind: str = "html"  # type: ignore[assignment]
            formats: list[str] = ["markdown"]

            def render(self, document, format, **options):
                return document.html.title.upper()

        wc.use(Shouty())
        print("plugin:     ", doc.render("markdown"))

        # [M3] Sessions: identity (cookies/headers) spanning fetches, with a
        #      ttl'd lifecycle. Cookies set by responses persist; sessions
        #      are isolated from each other.
        session = wc.session(ttl=300, headers={"x-app": "demo"})
        session.ref(f"{base}/login").fetch()
        print("session:    ", session.status, "cookies:", session.cookies)

        # [M3] paginate(): walk a next-link chain (selector, callable, or
        #      iterable of params), with until/limit/offset/resume knobs.
        feed = session.ref(f"{base}/feed").fetch()
        for page_doc in feed.paginate("a.next", limit=3):
            print("page:       ", page_doc.select("h1").text)

        # [M4] browser=True -> a LiveDocument backed by a real page. Actions
        #      auto-wait and are recorded; the DOM/console/network are
        #      captured onto the document as events.
        live = wc.ref(f"{base}/app").fetch(browser=True)
        live.write("#qty", "3").click("#add")
        live.wait_for("#cart li", timeout=5.0)
        print("live dom:   ", live.select("#cart li").text)
        print("console:    ", [e.text for e in live.console])
        print("dom events: ", len(live.dom_mutations), "mutations captured")

        # [M4] LiveNode event narrowing: an element sees only its own subtree.
        cart = live.select("#cart")
        print("narrowed:   ", len(cart.events_of(DOMUpdateEvent)), "under #cart")

        # [M4] A recording replays onto a fresh page to reproduce state.
        actions = list(live.actions)
        wc.release(live)                            # page back to the pool
        replayed = wc.ref(f"{base}/app").fetch(browser=True)
        replayed.replay(actions)
        print("replayed:   ", replayed.select("#cart li", optional=True) is not None)
        wc.release(replayed)

        # [M2] Pool stats: bounded leases over http clients AND browser pages.
        print("pool:       ", wc.pool.stats())

    # [M5] Lazy plans: the same interface, recorded not executed. Build a
    #      declarative extraction pipeline; it serializes to a QueryPlan --
    #      the wire format for the future HTTP/websocket API.
    plan = (
        q.ref.fetch().select_all(".card")
        .map(
            title=q.node.select(".title").text,
            price=q.node.select(".price").text,
            link=q.node.select("a").attr("href"),
        )
        .filter(q.col("price") != "")
    )
    print("\nlazy plan:")
    print("  " + plan.explain().replace("\n", "\n  "))
    print("wire form:  ", plan.to_query().model_dump_json()[:70], "...")

    # [M6] Executor: run the plan. Fetches lease from the pool, map() fans
    #      out per element with bounded concurrency, rows stream as ready.
    with WebClient() as wc:
        rows = plan.collect(wc.ref(f"{base}/"))
        for row in rows:
            print(f"  row:       {row['title']} {row['price']} "
                  f"-> {row['link'].path}")

        # [M6] A pipeline that follows each card's link (then + col) and
        #      pulls a field from the JSON detail page -- streamed.
        enriched = (
            q.ref.fetch().select_all(".card")
            .map(title=q.node.select(".title").text,
                 link=q.node.select("a.link").attr("href"))
            .then(name=q.col("link").fetch().json.query("name"))
        )
        print("streamed:")
        for row in enriched.collect(wc.ref(f"{base}/"), stream=True):
            print("  detail:   ", row["title"], "->", row["name"])
    # [M7] The same WebClient behind an HTTP API -- browser as a service.
    #      Documents are handles; content crosses the wire only via /render
    #      or /select, and plans are submitted as QueryPlan JSON.
    from fastapi.testclient import TestClient

    from webclient.service import create_app

    with TestClient(create_app(token="demo")) as api:
        auth = {"Authorization": "Bearer demo"}
        meta = api.post("/fetch", json={"url": f"{base}/"}, headers=auth).json()
        print("\nservice fetch:", {k: meta[k] for k in ("kind", "ok", "title")})
        md = api.get(f"/documents/{meta['id']}/render",
                     params={"format": "markdown"}, headers=auth).json()
        print("service render:", md["result"].splitlines()[0])
        titles = api.post(f"/documents/{meta['id']}/select",
                          json={"selector": ".title", "all": True},
                          headers=auth).json()
        print("service select:", titles["values"])
        plan = q.ref.fetch().select_all(".card").map(
            title=q.node.select(".title").text).to_query()
        rows = api.post("/plans", params={"url": f"{base}/"},
                        json=plan.model_dump(), headers=auth).json()
        print("service plan:  ", rows["rows"])

    # [remote] The RemoteWebClient drives that same service over HTTP with no
    #      local browser or lxml -- httpx + pydantic only. The plan API is
    #      identical to the local client; documents are handles.
    import threading
    import time

    import uvicorn

    from webclient import RemoteWebClient

    app = create_app(token="demo")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0,
                                           log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]

    with RemoteWebClient(f"http://127.0.0.1:{port}", token="demo") as rc:
        doc = rc.ref(f"{base}/").fetch()
        print("\nremote fetch:  ", doc.title, "| ok:", doc.ok)
        print("remote render: ", doc.markdown.splitlines()[0])
        print("remote select: ", doc.select_all(".title"))
        # identical plan API -- runs server-side, no local browser/lxml
        same_plan = q.ref.fetch().select_all(".card").map(
            title=q.node.select(".title").text)
        print("remote plan:   ", same_plan.collect(rc.ref(f"{base}/")))
    server.should_exit = True
    app.state.wc.close()


if __name__ == "__main__":
    main()


class LazyExpr:
    """A lazy expression tree that builds a QueryPlan instead of executing."""

    is_lazy: bool = True # Swaps the runtime behaviour of OPs to be lazy

    def map(self, **kwargs) -> LazyExpr:
        """Map a dict of column names to lazy expressions."""
        raise NotImplementedError("map must be implemented by subclasses")

    def project(self, *columns) -> LazyExpr:
        """Project a subset of columns from the lazy expression."""
        raise NotImplementedError("project must be implemented by subclasses")

    def filter(self, condition) -> LazyExpr:
        """Filter rows based on a lazy condition expression."""
        raise NotImplementedError("filter must be implemented by subclasses")

    def optional(self, mode: Literal["skip", "keep", "error"] = "keep") -> LazyExpr:
        """Mark the lazy expression as optional (lenient)."""
        raise NotImplementedError("optional must be implemented by subclasses")

    def require(self, mode: Literal["skip", "keep", "error"] = "skip") -> LazyExpr:
        """Require that certain columns must be present in the lazy expression."""
        raise NotImplementedError("require must be implemented by subclasses")


    def to_query(self) -> dict:
        """Serialize the lazy expression to a QueryPlan dict."""
        raise NotImplementedError("to_query must be implemented by subclasses")

    def explain(self) -> str:
        """Return a human-readable explanation of the lazy expression."""
        raise NotImplementedError("explain must be implemented by subclasses")

    
class LazyDocument(Document, LazyExpr): ...
    # stubs go here; the actual implementation is in the Document class, all methods are typed to return Lazy classes / LazyValue

doc = LazyDocument()
ref = LazyReference()
doc.select_all(".card").map(
    title=doc.select(".title").text,
    price=doc.select(".price").text,
    link=doc.select("a").attr("href"),
).filter(field("price") != "")

# Lazy items can also be rooted -> they dont need additional runtime context to be executed
ref("http://example.com").fetch().select_all(".card").map(
    title=doc.select(".title").text,
    price=doc.select(".price").text,
    link=doc.select("a").attr("href"),
).filter(field("price") != "")

# Similarly documents can be rooted to an ID  to reference a doc already loaded, if non is specified it will be assumed to be the current document from the context
doc("1234").select_all(".card").map(
    title=doc.select(".title").text,
    price=doc.select(".price").text,
    link=doc.select("a").attr("href"),
).filter(field("price") != "")