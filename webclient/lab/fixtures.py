"""The lab's fixtures: one per feature, each with its page(s) and its EXPECTED result.

A :class:`Fixture` owns a URL subtree (``/lab/<name>`` and anything beneath), answers
requests for it (``handle``), and publishes what a correct client should find
(``expected``). Keep each fixture SMALL and explicit -- the point is that a regression is
obvious -- and add one whenever a feature ships (roadmap D6).
"""

from __future__ import annotations

import json
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable

Response = "tuple[int, dict[str, str], bytes]"
Query = dict[str, list[str]]

HTML = {"Content-Type": "text/html; charset=utf-8"}
JSON = {"Content-Type": "application/json"}
XML = {"Content-Type": "application/rss+xml"}


def html(body: str, *, status: int = 200, headers: "dict[str, str] | None" = None) -> "tuple[int, dict[str, str], bytes]":
    return status, {**HTML, **(headers or {})}, body.encode("utf-8")


def as_json(value: Any, *, status: int = 200, headers: "dict[str, str] | None" = None) -> "tuple[int, dict[str, str], bytes]":
    return status, {**JSON, **(headers or {})}, json.dumps(value).encode()


def page(title: str, body: str, *, head: str = "") -> str:
    return f"<!doctype html><html><head><title>{title}</title>{head}</head><body>{body}</body></html>"


@dataclass
class Fixture:
    """One lab fixture: its ``name`` (the ``/lab/<name>`` path), what it strains, whether a
    browser is needed, the request handler, and the expected result."""

    name: str
    title: str
    feature: str
    handler: "Callable[[str, str, Query, dict[str, str], bytes], tuple[int, dict[str, str], bytes] | None]"
    expected: dict[str, Any] = field(default_factory=dict)
    browser: bool = False

    @property
    def path(self) -> str:
        return f"/lab/{self.name}"

    def handle(self, method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> "tuple[int, dict[str, str], bytes] | None":
        if path == self.path or path.startswith(self.path + "/") or path in _EXTRA.get(self.name, ()):
            return self.handler(method, path, query, headers, body)
        return None


FIXTURES: dict[str, Fixture] = {}
_EXTRA: dict[str, tuple[str, ...]] = {}  # extra top-level paths a fixture owns (/robots.txt, ...)


def fixture(name: str, title: str, feature: str, *, expected: "dict[str, Any] | None" = None,
            browser: bool = False, extra: "tuple[str, ...]" = ()) -> Callable[[Any], Any]:
    def wrap(fn: Any) -> Any:
        FIXTURES[name] = Fixture(name, title, feature, fn, expected or {}, browser)
        if extra:
            _EXTRA[name] = extra
        return fn
    return wrap


# --------------------------------------------------------------------------- #
# the product landing page (content, not a fixture)
# --------------------------------------------------------------------------- #

def landing() -> bytes:
    return page("webclient", """
<main>
<h1>webclient</h1>
<p>A declarative web client: fetch pages, select and extract structured data, render to
markdown, drive a real browser, and run the <em>same plan</em> sync, async or remote.</p>
<ul>
 <li><a href="/lab">The lab</a> — every feature has a fixture page and an expected result.</li>
 <li><a href="/lab/index.json">index.json</a> — the fixtures, machine-readable.</li>
</ul>
</main>""").encode()


# --------------------------------------------------------------------------- #
# static record list (extract / patterns / crawl seed)
# --------------------------------------------------------------------------- #

PRODUCTS = [("Aeropress", "39.00", 1), ("Grinder", "129.00", 2), ("Gooseneck Kettle", "59.00", 3)]


def _cards() -> str:
    return "".join(
        f'<div class="card" data-rank="{i}"><h2 class="title">{n}</h2>'
        f'<a class="link" href="/lab/shop/items/{i}">view</a>'
        f'<span class="price" data-price="{p}">${p.split(".")[0]}</span></div>'
        for n, p, i in PRODUCTS
    )


@fixture("shop", "A static shop listing (records, links, prices)", "extract",
         expected={"records": 3, "record_selector": "div.card", "titles": [n for n, _, _ in PRODUCTS],
                   "prices": [p for _, p, _ in PRODUCTS], "flags": [], "kind": "html", "tier": "static",
                   "links": ["/lab/shop/items/1", "/lab/shop/items/2", "/lab/shop/items/3", "/lab/about", "/lab/login"]})
def _shop(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    if path.startswith("/lab/shop/items/"):
        n = int(path.rsplit("/", 1)[1])
        name = next((nm for nm, _, i in PRODUCTS if i == n), "Item")
        return as_json({"id": n, "name": name, "stock": {"count": 7 * n}, "sku": f"SKU-{n}"})
    return html(page("Roasters Coffee", f"""
<nav><a href="/lab/about">about</a><a href="/lab/login">sign in</a></nav>
<main><h1>Featured</h1>{_cards()}</main>
<footer>(c) Roasters</footer>"""))


@fixture("about", "A plain page", "fetch", expected={"title": "About", "kind": "html"})
def _about(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    return html(page("About", "<main><h1>About</h1><p>We roast.</p></main>"))


# --------------------------------------------------------------------------- #
# JS-gated SPA (auto escalation), XHR-backed feed (SPA + data API)
# --------------------------------------------------------------------------- #

@fixture("spa", "A JS-gated SPA: an empty shell a script fills", "signals:spa", browser=True,
         expected={"static_flags": ["spa"], "remedy": "browser", "records_static": 0, "records_rendered": 3,
                   "tiers_auto": ["static", "browser"]})
def _spa(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    return html(page("SPA", """
<div id="app"></div>
<script>
  document.getElementById("app").innerHTML = '<ul class="news">' +
    [1,2,3].map(function(i){ return '<li class="item"><h3>Story ' + i +
      '</h3><time datetime="2026-09-0' + i + '">Sep ' + i + '</time></li>'; }).join('') + '</ul>';
</script>"""))


FEED_ITEMS = [{"title": "Q3 earnings released", "date": "2026-09-14"},
              {"title": "New roastery opens", "date": "2026-09-15"},
              {"title": "Partnership announced", "date": "2026-09-16"}]


@fixture("feed", "An XHR-backed feed: the records come from a JSON API", "signals:spa+xhr", browser=True,
         expected={"api": "/lab/feed/api/items", "records_rendered": 3, "xhr_endpoints": ["/lab/feed/api/items"],
                   "items": FEED_ITEMS})
def _feed(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    if path == "/lab/feed/api/items":
        return as_json(FEED_ITEMS)
    return html(page("Newsroom", """
<main><ul id="list"></ul></main>
<script>
  fetch('/lab/feed/api/items').then(function(r){ return r.json(); }).then(function(items){
    document.getElementById('list').innerHTML = items.map(function(it){
      return '<li class="item"><h3>' + it.title + '</h3><time datetime="' + it.date + '">' + it.date + '</time></li>';
    }).join('');
  });
</script>"""))


# --------------------------------------------------------------------------- #
# pagination: rel=next pages, a page param, a cursor API
# --------------------------------------------------------------------------- #

PAGES = 3
PER_PAGE = 4


def _rows(pageno: int) -> str:
    start = (pageno - 1) * PER_PAGE
    return "".join(
        f'<article class="row"><span class="n">{start + i + 1}</span><span class="name">Row {start + i + 1}</span></article>'
        for i in range(PER_PAGE)
    )


@fixture("paginated", "A dataset across pages: rel=next + ?page=", "pagination",
         expected={"pages": PAGES, "per_page": PER_PAGE, "total": PAGES * PER_PAGE, "record_selector": "article.row",
                   "flags": ["pagination"], "next_of_1": "/lab/paginated?page=2"})
def _paginated(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    pageno = int((query.get("page") or ["1"])[0])
    if pageno < 1 or pageno > PAGES:
        return html(page("Not found", "<h1>No such page</h1>"), status=404)
    nxt = f'<a rel="next" href="/lab/paginated?page={pageno + 1}">next</a>' if pageno < PAGES else ""
    prev = f'<a rel="prev" href="/lab/paginated?page={pageno - 1}">prev</a>' if pageno > 1 else ""
    link = {"Link": f'</lab/paginated?page={pageno + 1}>; rel="next"'} if pageno < PAGES else {}
    return html(page(f"Rows p{pageno}", f"""
<main><h1>Rows</h1>{_rows(pageno)}
<nav class="pagination">{prev} <span class="current">{pageno}</span> {nxt}</nav></main>"""), headers=link)


@fixture("cursor", "A keyset-paginated JSON API (?after=)", "pagination:cursor",
         expected={"total": 10, "page_size": 4, "cursor_path": "pageInfo.endCursor"})
def _cursor(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    after = int((query.get("after") or ["0"])[0])
    items = [{"id": i, "name": f"Item {i}"} for i in range(after + 1, min(after + 4, 10) + 1)]
    end: "int | None" = int(items[-1]["id"]) if items else None  # type: ignore[call-overload]
    more = end is not None and end < 10
    return as_json({"items": items, "pageInfo": {"endCursor": end if more else None, "hasNext": more}})


# --------------------------------------------------------------------------- #
# tabs, shadow DOM, iframe, forms (structure signals)
# --------------------------------------------------------------------------- #

@fixture("tabs", "Content split across ARIA tabs", "signals:tabbed",
         expected={"flags": ["tabbed"], "tabs": ["Upcoming", "Past"], "records": 4})
def _tabs(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    return html(page("Events", """
<main>
<div role="tablist"><button role="tab" aria-selected="true">Upcoming</button><button role="tab">Past</button></div>
<div role="tabpanel"><ul><li class="event">Roast Day</li><li class="event">Cupping</li></ul></div>
<div role="tabpanel" hidden><ul><li class="event">Launch</li><li class="event">Harvest</li></ul></div>
</main>"""))


@fixture("shadow", "Records inside a shadow root", "signals:shadow_dom", browser=True,
         expected={"flags_static": ["shadow_dom"], "records_rendered": 2})
def _shadow(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    return html(page("Shadow", """
<main><product-list></product-list></main>
<script>
  class ProductList extends HTMLElement { connectedCallback() {
    const root = this.attachShadow({mode: 'open'});
    root.innerHTML = '<ul><li class="p">Alpha</li><li class="p">Beta</li></ul>';
  } }
  customElements.define('product-list', ProductList);
</script>"""))


@fixture("iframe", "Records inside a same-origin iframe", "signals:iframe", browser=True,
         expected={"flags_static": ["iframe"], "records_rendered": 2})
def _iframe(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    if path == "/lab/iframe/inner":
        return html(page("Inner", '<ul><li class="q">One</li><li class="q">Two</li></ul>'))
    return html(page("Framed", '<main><h1>Framed</h1><iframe src="/lab/iframe/inner"></iframe></main>'))


@fixture("forms", "A search form + buttons", "signals:forms",
         expected={"flags": ["forms", "buttons"], "form_fields": ["q", "sort"]})
def _forms(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    if method == "POST" or query.get("q"):
        q = (query.get("q") or [body.decode("utf-8", "replace").split("q=")[-1].split("&")[0] if body else ""])[0]
        return html(page("Results", f'<main><h1>Results for {q}</h1><ul><li class="r">hit</li></ul></main>'))
    return html(page("Search", """
<main><form action="/lab/forms" method="get"><input name="q" type="search" placeholder="search">
<select name="sort"><option>new</option><option>old</option></select><button type="submit">Go</button></form>
<button id="more" onclick="this.textContent='Loaded'">Load more</button></main>"""))


# --------------------------------------------------------------------------- #
# walls: login, anti-bot interstitial, redirects, errors
# --------------------------------------------------------------------------- #

@fixture("login", "A sign-in wall (password form on a sparse page)", "signals:login",
         expected={"flags": ["login_present", "login_required", "forms", "buttons"], "auto_error": "fetch.login_required"})
def _login(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    if method == "POST":
        return html(page("Welcome", "<main><h1>Signed in</h1></main>"),
                    headers={"Set-Cookie": "sid=abc123; Path=/"})
    if headers.get("Cookie", "").find("sid=abc123") >= 0:
        return html(page("Account", '<main><h1>Account</h1><p class="who">you@example.com</p></main>'))
    return html(page("Sign in", """
<form action="/lab/login" method="post"><input name="email" type="email" placeholder="you@example.com">
<input name="password" type="password"><button type="submit">Sign in</button></form>"""))


@fixture("antibot", "A Cloudflare-style challenge interstitial (403)", "signals:anti_bot",
         expected={"flags": ["anti_bot_triggered", "anti_bot_present"], "remedy": "stealth", "status": 403})
def _antibot(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    return html(page("Just a moment...", """
<div id="cf-browser-verification"><h1>Just a moment...</h1><p>Checking your browser before accessing the site.</p></div>"""),
                status=403, headers={"cf-ray": "8f0c1234abcd-LHR", "cf-mitigated": "challenge",
                                     "Set-Cookie": "__cf_bm=x; Path=/"})


@fixture("redirect", "A redirect chain (302 -> 301 -> 200)", "transport:redirects",
         expected={"hops": 2, "final": "/lab/redirect/final", "title": "Landed"})
def _redirect(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    if path == "/lab/redirect":
        return 302, {"Location": "/lab/redirect/hop", "Content-Type": "text/plain"}, b""
    if path == "/lab/redirect/hop":
        return 301, {"Location": "/lab/redirect/final", "Content-Type": "text/plain"}, b""
    return html(page("Landed", "<main><h1>Landed</h1></main>"))


@fixture("errors", "Error responses: 404 / 500 / 503 (retriable)", "errors",
         expected={"codes": {"404": "fetch.http_status", "500": "fetch.http_status", "503": "fetch.http_status"},
                   "retriable": {"404": False, "500": True, "503": True}})
def _errors(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    code = path.rsplit("/", 1)[-1]
    if code.isdigit():
        return html(page(f"Error {code}", f"<h1>{code}</h1>"), status=int(code))
    return html(page("Errors", '<ul><li><a href="/lab/errors/404">404</a></li><li><a href="/lab/errors/500">500</a></li></ul>'))


@fixture("slow", "A slow endpoint (?delay= seconds, default 2)", "resiliency:timeout",
         expected={"default_delay_s": 2})
def _slow(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    time.sleep(float((query.get("delay") or ["2"])[0]))
    return html(page("Slow", "<main><h1>Finally</h1></main>"))


# --------------------------------------------------------------------------- #
# document kinds: JSON, XML (RSS), PDF, a large document; discovery: sitemap + robots
# --------------------------------------------------------------------------- #

@fixture("api", "A JSON document (nested records)", "kind:json",
         expected={"kind": "json", "path": "data.items", "count": 3, "first_name": "Aeropress"})
def _api(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    return as_json({"data": {"items": [{"id": i, "name": n, "price": {"value": float(p), "currency": "USD"}}
                                       for n, p, i in PRODUCTS], "total": 3}})


@fixture("rss", "An RSS feed (XML)", "kind:xml",
         expected={"kind": "xml", "items": 3, "first_title": "Q3 earnings released"})
def _rss(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    items = "".join(f"<item><title>{it['title']}</title><link>/lab/feed</link><pubDate>{it['date']}</pubDate></item>"
                    for it in FEED_ITEMS)
    return 200, XML, f'<?xml version="1.0"?><rss version="2.0"><channel><title>Newsroom</title>{items}</channel></rss>'.encode()


_PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 100]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")


@fixture("pdf", "A PDF document (binary)", "kind:binary", expected={"kind": "binary", "content_type": "application/pdf"})
def _pdf(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    return 200, {"Content-Type": "application/pdf"}, _PDF


@fixture("large", "A very large document (6000 records, ~600 KB)", "signals:large_document",
         expected={"records": 6000, "flags": ["large_document"], "record_selector": "li.item"})
def _large(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    items = "".join(f'<li class="item" data-i="{i}"><span class="k">{i}</span><span class="v">value {i} of a long, repetitive listing</span></li>' for i in range(6000))
    return html(page("Large", f"<main><ul>{items}</ul></main>"))


@fixture("gzip", "A gzip-encoded response", "transport:encoding", expected={"title": "Zipped"})
def _gzip(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    raw = page("Zipped", "<main><h1>Zipped</h1></main>").encode()
    co = zlib.compressobj(wbits=31)
    return 200, {**HTML, "Content-Encoding": "gzip"}, co.compress(raw) + co.flush()


SITE_PAGES = ["/lab/shop", "/lab/about", "/lab/paginated", "/lab/tabs", "/lab/forms"]


@fixture("sitemap", "sitemap.xml + robots.txt discovery", "crawl:sitemap", extra=("/robots.txt", "/sitemap.xml"),
         expected={"sitemap_urls": SITE_PAGES, "disallow": ["/lab/login", "/lab/private"], "sitemap_in_robots": True})
def _sitemap(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    origin = f"http://{headers.get('Host') or headers.get('host') or '127.0.0.1'}"  # absolute, per the spec
    if path == "/robots.txt":
        return 200, {"Content-Type": "text/plain"}, (
            f"User-agent: *\nDisallow: /lab/login\nDisallow: /lab/private\nSitemap: {origin}/sitemap.xml\n".encode())
    urls = "".join(f"<url><loc>{origin}{u}</loc></url>" for u in SITE_PAGES)
    return 200, {"Content-Type": "application/xml"}, f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'.encode()


# --------------------------------------------------------------------------- #
# the live app (interaction, recording, tracing, rrweb)
# --------------------------------------------------------------------------- #

@fixture("app", "A live cart app: type, click, rows appear (XHR-backed)", "interact", browser=True,
         expected={"controls": ["#qty", "#add", "#load"], "after_add_rows": 1, "after_load_rows": 3})
def _app(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    if path == "/lab/app/api/items":
        return as_json([{"name": "Aeropress"}, {"name": "Grinder"}, {"name": "Kettle"}])
    return html(page("Cart", """
<h1>Cart</h1>
<input id="qty" type="text" placeholder="qty">
<button id="add" onclick="document.querySelector('#cart').insertAdjacentHTML('beforeend', '<li class=row>item x' + document.querySelector('#qty').value + '</li>')">add</button>
<button id="load" onclick="fetch('/lab/app/api/items').then(r=>r.json()).then(j=>{document.querySelector('#cart').innerHTML = j.map(i=>'<li class=row>'+i.name+'</li>').join('')})">load</button>
<ul id="cart"></ul>"""))


@fixture("scroll", "Infinite scroll: more rows load on scroll", "interact:scroll", browser=True,
         expected={"initial_rows": 5, "after_scroll_rows": 10})
def _scroll(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    return html(page("Scroll", """
<main><ul id="rows">""" + "".join(f'<li class="r" style="height:400px">Row {i}</li>' for i in range(1, 6)) + """</ul></main>
<script>
  let n = 5;
  window.addEventListener('scroll', function(){
    if (n >= 10) return;
    if (window.innerHeight + window.scrollY >= document.body.offsetHeight - 50) {
      for (let i = 0; i < 5; i++) { n++; const li = document.createElement('li'); li.className='r'; li.style.height='400px'; li.textContent='Row ' + n; document.getElementById('rows').appendChild(li); }
    }
  });
</script>"""))
