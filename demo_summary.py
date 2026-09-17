"""demo_summary.py -- the library from an LLM's point of view.

Where ``demo.py`` is a terse feature checklist, this walks each capability the way
an agent would actually use it: it prints, for every feature, the call an LLM would
write, the text the LLM gets back, and a one-line use case. The goal is to show
*what the model sees* -- the compact, readable projections (``print(summary)``,
``print(crawl)``, skeletons, lazy plans) the library is built to hand an LLM.

Runs fully offline (serves its own site on localhost):

    env/bin/python demo_summary.py
"""

from __future__ import annotations

import http.server
import threading
from typing import Any

from webclient import RETURN, WebClient, from_blob, wq

# --------------------------------------------------------------------------- #
# A small offline "Acme Newsroom" site: a rich article page, some linked pages
# (for the crawl), a JSON data API, a robots.txt + sitemap, and a results page.
# --------------------------------------------------------------------------- #

ARTICLE = b"""
<html lang="en"><head>
  <title>Acme launches Widget 3</title>
  <meta name="description" content="Acme today unveiled Widget 3, its fastest widget yet.">
  <meta property="og:title" content="Acme launches Widget 3">
  <link rel="canonical" href="/news/2026/09/widget-3">
  <link rel="alternate" type="application/rss+xml" href="/feed.xml">
  <script type="application/ld+json">{"@type":"NewsArticle","headline":"Acme launches Widget 3"}</script>
</head><body>
  <header><nav>
    <a href="/products">Products</a>
    <a href="/news">Newsroom</a>
    <a href="/about">About</a>
  </nav></header>
  <main>
    <h1>Acme launches Widget 3</h1>
    <h2>What's new</h2>
    <article class="post">
      <p>Acme today announced <strong>Widget 3</strong>, its fastest widget yet,
         with a redesigned motor and a lighter frame.</p>
      <a class="cta" href="/news/2026/09/widget-3-full-announcement">Read more</a>
      <span class="price" data-testid="price">$49</span>
    </article>
    <h2>Specifications</h2>
    <ul><li>Weight: 1kg</li><li>Battery: 10 hours</li></ul>
    <form method="post" action="/subscribe">
      <input name="email" placeholder="you@example.com"><button>Subscribe</button>
    </form>
  </main>
  <footer>
    <a href="/privacy">Privacy Policy</a>
    <a href="/terms">Terms of Use</a>
    <a href="https://twitter.com/acme">Follow us on Twitter</a>
    <a href="/logo.png"><img src="/logo.png" alt="logo"></a>
  </footer>
</body></html>
"""

LINKED = b"<html><head><title>%s</title></head><body><h1>%s</h1><p>%s</p></body></html>"
ITEM = b'{"id": 3, "name": "Widget 3", "price": 49, "stock": {"count": 7, "warehouse": "EU"}}'
SEARCH = b"""
<html><body>
  <div class="result result--ad"><span>Sponsored</span></div>
  <div class="result">
    <a class="result__a" href="https://acme.example/widget-3">Widget 3 review</a>
    <a class="result__snippet">Our hands-on with Acme's fastest widget.</a>
  </div>
  <div class="result">
    <a class="result__a" href="https://acme.example/widget-3-specs">Widget 3 specs</a>
    <a class="result__snippet">Weight, battery, and price for Widget 3.</a>
  </div>
</body></html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    ROUTES = {
        "/products": (b"Products", b"Our products", b"Widget 1, Widget 2, Widget 3."),
        "/news": (b"Newsroom", b"Newsroom", b"All the latest from Acme."),
        "/about": (b"About Acme", b"About", b"Acme builds widgets."),
        "/news/2026/09/widget-3-full-announcement": (
            b"Widget 3 announcement", b"Widget 3", b"The full announcement text."),
        "/subscribe": (b"Subscribed", b"Thanks", b"You are subscribed."),
    }

    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/news/2026/09/widget-3"):
            return self._send(ARTICLE, "text/html; charset=utf-8")
        if self.path == "/api/item/3":
            return self._send(ITEM, "application/json")
        if self.path.startswith("/search"):
            return self._send(SEARCH, "text/html; charset=utf-8")
        if self.path == "/robots.txt":
            return self._send(
                f"User-agent: *\nDisallow: /private\nSitemap: {self._base()}/sitemap.xml\n".encode(),
                "text/plain",
            )
        if self.path == "/sitemap.xml":
            base = self._base()
            return self._send(
                (
                    '<?xml version="1.0"?>'
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    f"<url><loc>{base}/news</loc></url>"
                    f"<url><loc>{base}/products</loc></url></urlset>"
                ).encode(),
                "application/xml",
            )
        if self.path in self.ROUTES:
            return self._send(LINKED % self.ROUTES[self.path], "text/html")
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"not found")

    def _base(self) -> str:
        return f"http://{self.headers.get('Host', '127.0.0.1')}"

    def _send(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Server", "nginx")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a: Any) -> None:  # quiet
        pass


def serve() -> str:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}"


# --------------------------------------------------------------------------- #
# Presentation helpers -- keep the tour readable.
# --------------------------------------------------------------------------- #


def panel(title: str, use_case: str) -> None:
    print("\n" + "=" * 74)
    print(f" {title}")
    print(f" USE CASE: {use_case}")
    print("=" * 74)


def call(code: str) -> None:
    print(f">>> {code}")


def out(text: Any) -> None:
    for line in str(text).splitlines() or [""]:
        print(f"    {line}")


def main() -> None:
    base = serve()
    with WebClient(default_headers={"user-agent": "webclient-demo"}) as wc:
        doc = wc.fetch(f"{base}/").collect()

        # 1 -------------------------------------------------------------------
        panel(
            "1. FACETS -- the LLM's primary view of a page",
            "orient on any URL in a few tokens before deciding how to extract.",
        )
        call("doc.transport()  /  doc.metadata()  /  doc.structure()")
        out(doc.transport())
        out(doc.metadata())
        out(doc.structure())
        print()
        print("    # each facet is an independent op; compose the ones you need per page.")

        # 2 -------------------------------------------------------------------
        panel(
            "2. SKELETON -- a token-lean DOM outline",
            "let the model author precise CSS selectors without the full HTML.",
        )
        call("doc.skeleton(max_lines=18, legend=False)")
        out(doc.skeleton(max_lines=18, legend=False))

        # 3 -------------------------------------------------------------------
        panel(
            "3. TARGETED EXTRACTION -- select / attr / region",
            "pull exact values once the model knows the selector.",
        )
        call('doc.select("h1").attr("text")')
        out(doc.select("h1").attr("text"))
        call('doc.select(".price").attr("text")')
        out(doc.select(".price").attr("text"))
        call('doc.select("a.cta").attr("href").url    # relative -> absolute')
        out(doc.select("a.cta").attr("href").url)
        call('doc.select("a.cta").region              # which page landmark?')
        out(doc.select("a.cta").region)
        call('[a.attr("text") for a in doc.select_all("nav a")]')
        out([a.attr("text") for a in doc.select_all("nav a")])

        # 4 -------------------------------------------------------------------
        panel(
            "4. READABLE RENDERS -- markdown / text / links",
            "hand the model clean prose instead of raw HTML tags.",
        )
        call("doc.markdown(main_content_only=True)")
        out(doc.markdown(main_content_only=True))
        call("[r.url for r in doc.links()][:4]")
        out([r.url for r in doc.links()][:4])

        # 5 -------------------------------------------------------------------
        panel(
            "5. JSON / CONTENT SNIFFING -- one API for every content type",
            "read a data API with the same select() the model uses on HTML.",
        )
        api = wc.fetch(f"{base}/api/item/3").collect()
        call('api.kind    # sniffed from the response, not the URL')
        out(api.kind)
        call('api.select("stock.count").attr("text")')
        out(api.select("stock.count").attr("text"))

        # 6 -------------------------------------------------------------------
        panel(
            "6. LAZY QUERIES -- a portable extraction plan",
            "the model writes a plan, ships it as a blob, runs it server-side.",
        )
        expr = wq.doc.select("article.post").select("a.cta").attr("href").url
        call("expr = wq.doc.select('article.post').select('a.cta').attr('href').url")
        call("expr.explain()")
        out(expr.explain())
        blob = expr.to_blob()
        call("blob = expr.to_blob()          # JSON, safe to send over the wire")
        out(blob[:88] + " ...")
        call("from_blob(blob).collect(doc)   # run the plan against a document")
        out(from_blob(blob).collect(doc))

        # 7 -------------------------------------------------------------------
        panel(
            "7. CRAWL -- a scored, sorted frontier",
            "map a site; the useful links (nav / 'read more') surface first.",
        )
        with wc.crawl(f"{base}/", max_pages=4, browser=False) as crawl:
            crawl.step()
            call("crawl.step(); print(crawl)")
            out(crawl)
        print()
        print("    # resource links (logo.png) dropped; footer/legal/social sink;")
        print("    # each edge keeps a .score, and the frontier is sorted by it.")

        # 8 -------------------------------------------------------------------
        panel(
            "8. SITEMAP / ROBOTS -- cheap hunts (dispatched IO ops, not crawls)",
            "hunt the sitemap.xml URLs and the robots.txt rules; crawl them if you like.",
        )
        call("[r.url for r in wc.sitemap(base)]")
        out([r.url for r in wc.sitemap(base)])
        call("wc.robots(base).sitemaps")
        out(wc.robots(base).sitemaps)

        # 9 ------------------------------------------------------------------
        panel(
            "10. ERRORS -- loud by default, opt-in lenient",
            "the model can trust a result, or ask for a miss it can branch on.",
        )
        call('wc.ref(f"{base}/nope").resolve(error=RETURN)   # lenient: a miss, no raise')
        miss = wc.ref(f"{base}/nope").resolve(error=RETURN)
        out(f"ok={miss.ok}  status={miss.status_code}  message={miss.message!r}")
        call('doc.select(".nonexistent", error=RETURN).ok')
        out(doc.select(".nonexistent", error=RETURN).ok)

    print("\n" + "=" * 74)
    print(" Every projection above is text an LLM can read directly -- that is the")
    print(" point of the library: turn a messy live web into compact, typed views.")
    print("=" * 74)


if __name__ == "__main__":
    main()
