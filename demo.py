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
    ActionEvent,
    NavigationEvent,
    Reference,
    Renderer,
    WebClient,
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


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/":                       # html page
            body, ctype = PAGE, "text/html; charset=utf-8"
        elif self.path == "/items/1":              # json documents
            body, ctype = ITEM % (1, b"Aeropress"), "application/json"
        elif self.path == "/items/2":
            body, ctype = ITEM % (2, b"Grinder"), "application/json"
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

        # [M2] Pool stats: bounded leases over persistent http clients.
        print("pool:       ", wc.pool.stats())

    # [M3] Sessions (cookies/identity spanning fetches) + paginate()  -- soon
    # [M4] browser=True -> LiveDocument: click/write/wait_for, live events,
    #      rrweb capture, LiveNode event narrowing                    -- soon
    # [M5] Lazy plans: q.ref.fetch().select_all(".card").map(...)     -- soon
    # [M6] Executor: wc.execute(plan, ref, stream=True)               -- soon
    # [M7] HTTP/WS service: browser as a service                      -- soon


if __name__ == "__main__":
    main()
