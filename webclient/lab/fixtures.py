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
from datetime import date, timedelta
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


# The webclient BRAND mark: a fan-out "plan graph" glyph (one node fanning to two -- the crawl /
# fan-out the package is built on) + the wordmark. One identity, shared verbatim with the
# webclient-ui design system so the lab and the app read as one product.
BRAND_MARK = (
    '<svg class="brand-mark" viewBox="0 0 24 24" width="19" height="19" fill="none" aria-hidden="true">'
    '<path d="M7.4 8.2 14.6 11 M7.4 15.8 14.6 13" stroke="var(--accent)" stroke-width="1.7" stroke-linecap="round"/>'
    '<circle cx="6" cy="7" r="2.5" fill="var(--accent)"/>'
    '<circle cx="6" cy="17" r="2.3" stroke="var(--accent)" stroke-width="1.7"/>'
    '<circle cx="17" cy="12" r="2.3" stroke="var(--accent)" stroke-width="1.7"/></svg>')


def brand(tag: str = "") -> str:
    """The wordmark: the mark + ``webclient`` + an optional muted tag (``lab`` / a page name)."""
    suffix = f'<span class="brand-tag">{tag}</span>' if tag else ""
    return f'<span class="brand">{BRAND_MARK}<span class="brand-word">webclient</span>{suffix}</span>'


def decoys(related: str = "") -> str:
    """Realistic page CHROME whose elements SHARE the record/field CSS classes -- a nav, a sidebar and
    a footer carrying ``.name`` / ``.price`` / ``.title`` / ``.date`` (and, when ``related`` is given,
    a "Related" panel of full record-shaped elements). It adds NOISE to the skeleton and would fool a
    naive TOP-LEVEL field selector, so a correct query must SELECT its records by a record wrapper
    inside the MAIN container and read fields RELATIVE to each record. A production page always has
    this; the fixtures include it so the model is tested against the same red herrings a real site has."""
    return (
        '<nav class="nav"><a class="name" href="#">Home</a> <a class="name" href="#">Deals</a>'
        ' <span class="title">Menu</span></nav>'
        '<aside class="sidebar"><h3 class="title">Recently viewed</h3>'
        '<ul><li><span class="name">Sponsored pick</span> <span class="price">$0</span></li>'
        '<li><span class="name">Gift cards</span> <span class="price">$25</span></li></ul>'
        '<div class="promo"><span class="title">Newsletter</span> <time class="date">today</time></div>'
        + related + '</aside>'
        '<footer class="foot"><a class="name" href="#">Careers</a> <span class="price">© 2026</span>'
        ' <span class="title">Privacy</span></footer>'
    )


# A shared, eye-friendly theme injected into every fixture page. It uses the SAME semantic tokens
# and values as the webclient-ui design system (surface / ink / line / accent / muted), so the two
# sites are one brand. It styles the markup the fixtures already use and adds NO DOM, no anchors and
# no network request -- so it never changes a record count, a link set, a signal or a
# rendered/static comparison a test asserts. It carries NO brand / platform marker literals (the
# cookie-banner detector substring-scans the raw HTML, <style> included) and never force-shows a
# [hidden] panel. Fixtures that need a real signal put its markers in their OWN body.
LAB_CSS = """<style>
:root{--surface:#fff;--surface-2:#f6f7f9;--surface-3:#eceef2;--ink:#16181d;--ink-2:#3d4250;
--muted:#6b7280;--line:#e3e6eb;--line-2:#cdd2da;--accent:#2457e6;--accent-ink:#fff;--accent-soft:#e6edff;
--ok:#15803d;--warn:#b45309;--bad:#b91c1c;--radius:12px}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--surface:#0f1115;--surface-2:#161a21;
--surface-3:#1f2430;--ink:#e8eaf0;--ink-2:#b6bcc9;--muted:#8b93a3;--line:#262c38;--line-2:#343c4b;
--accent:#6b8cff;--accent-ink:#0b1020;--accent-soft:#1b2440;--ok:#4ade80;--warn:#fbbf24;--bad:#f87171}}
:root[data-theme=dark]{--surface:#0f1115;--surface-2:#161a21;--surface-3:#1f2430;--ink:#e8eaf0;
--ink-2:#b6bcc9;--muted:#8b93a3;--line:#262c38;--line-2:#343c4b;--accent:#6b8cff;--accent-ink:#0b1020;
--accent-soft:#1b2440;--ok:#4ade80;--warn:#fbbf24;--bad:#f87171}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0 auto;max-width:64rem;padding:2rem 1.25rem 4rem;color:var(--ink);background:var(--surface-2);
font:15px/1.65 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
h1{font-size:1.7rem;line-height:1.2;margin:.2rem 0 1rem;letter-spacing:-.015em}
h2{font-size:1.15rem;margin:1.6rem 0 .6rem;letter-spacing:-.01em}h3{font-size:1rem;margin:.35rem 0}
p{margin:.5rem 0}a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
small{color:var(--muted)}
code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.86em}
code{background:var(--surface-3);padding:.05rem .3rem;border-radius:4px}
pre{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:.9rem 1rem;overflow:auto}
pre code{background:none;padding:0}
.brand{display:inline-flex;align-items:center;gap:.5rem;font-weight:600}
.brand-mark{flex:none}
.brand-word{letter-spacing:-.02em}
.brand-tag{color:var(--muted);font-weight:500;border-left:1px solid var(--line-2);padding-left:.55rem;letter-spacing:.02em}
nav{display:flex;flex-wrap:wrap;gap:1.1rem;align-items:center;padding-bottom:.8rem;margin-bottom:1.4rem;border-bottom:1px solid var(--line)}
nav a{font-weight:550;color:var(--ink-2)}nav a:hover{color:var(--accent)}
footer{margin-top:2.5rem;padding-top:1rem;border-top:1px solid var(--line);color:var(--muted);font-size:.85rem}
ul{padding-left:1.2rem}li{margin:.15rem 0}
.card{display:flex;align-items:center;gap:.9rem;flex-wrap:wrap;padding:.8rem 1rem;margin:.6rem 0;border:1px solid var(--line);border-radius:var(--radius);background:var(--surface)}
.card .title,.card .name{font-weight:600}
.price{margin-left:auto;font-weight:650;background:var(--accent-soft);color:var(--accent);padding:.1rem .55rem;border-radius:999px;font-size:.85rem}
a.link{align-self:center;font-size:.85rem;border:1px solid var(--line);padding:.15rem .6rem;border-radius:7px;color:var(--ink-2)}
a.link:hover{border-color:var(--accent);color:var(--accent)}
article.row{display:flex;align-items:center;gap:.8rem;padding:.5rem .8rem;margin:.35rem 0;border:1px solid var(--line);border-radius:8px;background:var(--surface)}
article.row .n{font-variant-numeric:tabular-nums;color:var(--muted);min-width:2ch;text-align:right}
time{color:var(--muted);font-size:.85rem}
form{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;margin:.8rem 0}
input,select,button{font:inherit;padding:.45rem .7rem;border:1px solid var(--line);border-radius:8px;background:var(--surface);color:var(--ink)}
button{cursor:pointer}button[type=submit],.btn{background:var(--accent);color:var(--accent-ink);border-color:transparent;font-weight:550}
[role=tablist]{display:flex;gap:.4rem;border-bottom:1px solid var(--line);margin-bottom:.8rem}
[role=tab]{border:none;background:none;padding:.4rem .8rem;border-radius:8px 8px 0 0;color:var(--muted)}
[role=tab][aria-selected=true]{color:var(--ink);font-weight:600;box-shadow:inset 0 -2px 0 var(--accent)}
[hidden]{display:none!important}
table{border-collapse:collapse;width:100%;margin:.8rem 0;font-size:.92rem}
th,td{border:1px solid var(--line);padding:.45rem .7rem;text-align:left}
thead th{background:var(--accent-soft);color:var(--accent)}
iframe{border:1px solid var(--line);border-radius:var(--radius);max-width:100%}
.lab-notice-bar{position:fixed;left:0;right:0;bottom:0;background:var(--surface);border-top:1px solid var(--line);padding:1rem 1.25rem;display:flex;gap:1rem;align-items:center;justify-content:center;flex-wrap:wrap;box-shadow:0 -8px 24px rgba(16,24,40,.12)}
.lab-badge{display:inline-block;background:var(--accent-soft);color:var(--accent);border-radius:999px;padding:.05rem .5rem;font-size:.75rem;font-weight:600}
</style>"""


def page(title: str, body: str, *, head: str = "") -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{title}</title>{LAB_CSS}{head}</head><body>{body}</body></html>")


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
    return page("webclient", f"""
<nav>{brand()}<a href="/lab">the lab</a><a href="/lab/index.json">index.json</a></nav>
<main>
<h1>Everything is a plan.</h1>
<p>A declarative web client and LLM toolkit: fetch pages, select and extract structured data,
render to markdown, drive a real browser, crawl and paginate — and run the <em>same plan</em>
sync, async or remote. An op doesn't <em>do</em> the work; it <strong>records</strong> a
wire-safe, typed plan, so <code>RUN(t) = fold(plan, events[0..t])</code>: every run is
replayable, traceable and portable.</p>

<h2>The design, in five ideas</h2>
<ul>
 <li><strong>One plan, three modes.</strong> Sync, async and remote are dispatch modes over
   the same recorded plan — not three code paths.</li>
 <li><strong>Tiers that escalate on evidence.</strong> A cheap static fetch first; the client
   escalates to a real browser only when <em>signals</em> say it must (a JS-gated SPA, a
   consent wall, shadow DOM).</li>
 <li><strong>Signals → flags.</strong> Tiered, confidence-scored <em>signals</em> (evidence)
   combine into <em>flags</em> (conclusions: <code>spa</code>, <code>login_required</code>,
   <code>pagination</code>, …) that auto-remediation and the pipeline act on.</li>
 <li><strong>One bounded loop.</strong> Pagination, crawl, extract and interaction all derive
   from a single observe → decide → apply loop.</li>
 <li><strong>Traceable by construction.</strong> Every step stamps events (rrweb DOM,
   network facts correlated to the DOM, pool leases) — watch a run as it unfolds.</li>
</ul>

<h2>The lab</h2>
<p>This site <em>is</em> the test suite. Every feature has a fixture page under
<a href="/lab">/lab</a>, and each publishes its <strong>expected result</strong> as JSON — so
tests, demos and docs all assert against one set of facts.</p>
<p><a class="link" href="/lab">Browse the fixtures →</a></p>
</main>
<footer>webclient lab · a declarative web client &amp; LLM toolkit</footer>""").encode()


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
                   "links": ["/lab/shop/items/1", "/lab/shop/items/2", "/lab/shop/items/3", "/lab/about", "/lab/login"],
                   "item": "/lab/shop/items/2", "item_stock": 14})
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


# a listing where SOME rows carry a sold-out badge -- a brief for "in-stock only" needs a .filter()
STORE = [  # (name, price, sold_out)
    ("Ethiopia Yirgacheffe", "18", False), ("Colombia Huila", "16", True),
    ("Kenya AA", "20", False), ("Sumatra Mandheling", "17", True), ("Guatemala Antigua", "19", False),
]


@fixture("store", "A listing where some rows are sold out (needs a filter)", "extract:filter",
         expected={"record_selector": "li.product", "records": len(STORE),
                   "in_stock": sum(1 for _, _, s in STORE if not s), "sold_out_selector": "span.sold-out",
                   "in_stock_names": [n for n, _, s in STORE if not s]})
def _store(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    """A catalogue where only SOME rows carry a ``sold-out`` badge. A brief for the IN-STOCK products
    must ``.filter()`` the sold-out rows out (``~wq.doc.select("span.sold-out", optional=True).is_ok()``),
    not just extract every row -- the completeness of the answer depends on the filter, not the selector."""
    rows = "".join(
        f'<li class="product"><span class="name">{n}</span> <span class="price">${p}</span>'
        + ('<span class="sold-out">Sold out</span>' if sold else '') + '</li>'
        for n, p, sold in STORE
    )
    return html(page("Store", f'<main><h1>Coffee store</h1><ul class="catalogue">{rows}</ul></main>{decoys()}'))


# --------------------------------------------------------------------------- #
# a realistic multi-section site with RED HERRINGS -- the crawl/select must navigate decoy nav
# links (about/login), decoy DATASETS (careers, blog), a FEATURED teaser (a subset of the real
# listing), and per-item JSON endpoints (a queryable DRILL-DOWN that must not be chosen as the
# dataset). The real dataset is the paginated products listing; SKU/stock are on each item's page.
# --------------------------------------------------------------------------- #

ACME = [("Aeropress", "39", False), ("Grinder", "129", False), ("Kettle", "59", True),
        ("Scale", "49", False), ("Filters", "12", False), ("Carafe", "35", True)]  # 6 products, 2 sold out
ACME_PER = 3
ACME_JOBS = [("Barista", "London"), ("Head Roaster", "Berlin")]          # careers: a DECOY dataset
ACME_POSTS = [("How we roast", "2026-09-10"), ("Water chemistry", "2026-09-12")]  # blog: another decoy
#: a TRANSPOSED plans/feature matrix on /lab/acme/pricing -- the plans are the COLUMNS, so records
#: run across (needs .table(transpose=True)). A realistic pricing page a brief might target.
ACME_PLAN_HEAD = ["Plan", "Home", "Cafe", "Roastery"]
ACME_PLAN_ROWS = [("Price", ["$0", "$29", "$99"]), ("Seats", ["1", "5", "Unlimited"]),
                  ("Support", ["Community", "Email", "Dedicated"])]
#: the reviews dataset behind a native JSON KEYSET (cursor) API -- /lab/acme/api/reviews?after= walks
#: it (pageInfo.endCursor + hasNextPage), the /lab/acme/reviews page just renders the first window.
ACME_REVIEWS = [(f"Reviewer {i}", 1 + (i % 5), f"Review body {i}") for i in range(1, 12)]  # 11, page size 4
ACME_REVIEW_PER = 4


def _acme_nav() -> str:
    return ('<nav><a href="/lab/acme">home</a> <a href="/lab/acme/about">about</a> '
            '<a href="/lab/acme/careers">careers</a> <a href="/lab/acme/login">sign in</a> '
            '<a href="/lab/acme/blog">blog</a> <a href="/lab/acme/pricing">pricing</a> '
            '<a href="/lab/acme/reviews">reviews</a></nav>')


@fixture("acme", "A realistic multi-section company site: paginated catalogue + drill-downs, a "
                 "transposed pricing table, a JSON keyset reviews API, and red-herring decoys",
         "crawl:complex",
         expected={"listing": "/lab/acme/products", "record_selector": "section.catalogue article.product",
                   "products": len(ACME), "per_page": ACME_PER, "in_stock": sum(1 for _, _, s in ACME if not s),
                   "detail_link": "a.detail", "api_link": "a.data", "teaser_selector": "article.teaser",
                   "decoys": ["/lab/acme/about", "/lab/acme/careers", "/lab/acme/login", "/lab/acme/blog"],
                   "sku_of_1": "ACME-001",
                   "pricing": "/lab/acme/pricing", "pricing_table": "table.plans", "plans": ACME_PLAN_HEAD[1:],
                   "reviews": "/lab/acme/reviews", "reviews_api": "/lab/acme/api/reviews",
                   "reviews_total": len(ACME_REVIEWS), "reviews_page_size": ACME_REVIEW_PER,
                   "reviews_cursor_path": "pageInfo.endCursor"})
def _acme(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    """A small but REALISTIC company site -- the lab's FLAGSHIP, combining many hard shapes on one site.
    Real datasets a brief might target: ``/lab/acme/products`` (a paginated catalogue; each item's SKU +
    stock on its own HTML detail page and JSON endpoint), ``/lab/acme/pricing`` (a TRANSPOSED plans matrix
    -- plans are columns, needs ``.table(transpose=True)``), and ``/lab/acme/reviews`` (rendered from a
    native JSON KEYSET API ``/lab/acme/api/reviews?after=`` -- pageInfo.endCursor + hasNextPage). Red
    herrings throughout: the nav decoys (about/login), two DECOY datasets that also look scrapeable
    (careers = roles, blog = posts), a FEATURED teaser on the landing (a 2-item SUBSET of the catalogue),
    per-item ``/lab/acme/api/products/{id}`` JSON endpoints (queryable, but ONE record each -- a drill-down,
    not the dataset), and a "Related" side panel sharing the record class (scope to ``section.catalogue``)."""
    if path.startswith("/lab/acme/products/"):  # an item's HTML detail page (SKU in a spec table)
        k = int(path.rsplit("/", 1)[1])
        n, p, _s = ACME[k - 1]
        return html(page(n, f'{_acme_nav()}<main><h1>{n}</h1><table class="spec">'
                            f'<tr><th>SKU</th><td>ACME-{k:03d}</td></tr>'
                            f'<tr><th>Price</th><td>${p}</td></tr></table></main>{decoys()}'))
    if path.startswith("/lab/acme/api/products/"):  # an item's JSON endpoint (a single record -- a decoy)
        k = int(path.rsplit("/", 1)[1])
        _n, _p, sold = ACME[k - 1]
        return as_json({"id": k, "sku": f"ACME-{k:03d}", "stock": {"count": 0 if sold else 7 * k}})
    if path == "/lab/acme/about":
        return html(page("About Acme", f'{_acme_nav()}<main><h1>About</h1><p>We sell coffee gear.</p></main>'))
    if path == "/lab/acme/login":
        return html(page("Sign in", f'{_acme_nav()}<main><h1>Sign in</h1>'
                                    '<form><input name="email"><input type="password"></form></main>'))
    if path == "/lab/acme/careers":  # a DECOY dataset: roles, not products
        jobs = "".join(f'<li class="job"><span class="role">{r}</span> <span class="loc">{loc}</span></li>'
                       for r, loc in ACME_JOBS)
        return html(page("Careers", f'{_acme_nav()}<main><h1>Open roles</h1><ul>{jobs}</ul></main>{decoys()}'))
    if path == "/lab/acme/blog":  # another DECOY dataset: posts
        posts = "".join(f'<article class="post"><h3 class="title">{t}</h3><time>{d}</time></article>'
                        for t, d in ACME_POSTS)
        return html(page("Blog", f'{_acme_nav()}<main><h1>From the blog</h1>{posts}</main>{decoys()}'))
    if path == "/lab/acme/pricing":  # a TRANSPOSED plans matrix: plans are COLUMNS (needs .table(transpose=True))
        head = "<tr>" + "".join(f"<th>{h}</th>" for h in ACME_PLAN_HEAD) + "</tr>"
        rows = "".join("<tr><td>" + feat + "</td>" + "".join(f"<td>{v}</td>" for v in vals) + "</tr>"
                       for feat, vals in ACME_PLAN_ROWS)
        return html(page("Pricing", f'{_acme_nav()}<main><h1>Plans &amp; pricing</h1>'
                         f'<table class="plans"><thead>{head}</thead><tbody>{rows}</tbody></table></main>{decoys()}'))
    if path == "/lab/acme/api/reviews":  # a native JSON KEYSET (cursor) API: ?after=<id>, pageInfo.endCursor
        after = int((query.get("after") or ["0"])[0])
        window = [r for r in ACME_REVIEWS if int(r[0].split()[-1]) > after][:ACME_REVIEW_PER]
        items = [{"id": int(n.split()[-1]), "reviewer": n, "rating": stars, "body": b} for n, stars, b in window]
        last = int(window[-1][0].split()[-1]) if window else None  # the last id in this window (typed int)
        more = last is not None and any(int(r[0].split()[-1]) > last for r in ACME_REVIEWS)
        return as_json({"reviews": items, "pageInfo": {"endCursor": last if more else None, "hasNextPage": more}})
    if path == "/lab/acme/reviews":  # the reviews PAGE: renders the first window, links the cursor API
        first = ACME_REVIEWS[:ACME_REVIEW_PER]
        cards = "".join(f'<article class="review"><span class="who">{n}</span>'
                        f'<span class="stars">{"★" * stars}</span><p class="body">{b}</p></article>'
                        for n, stars, b in first)
        return html(page("Reviews", f'{_acme_nav()}<main><h1>Customer reviews</h1>'
                         f'<section class="reviews">{cards}</section>'
                         f'<p>Loaded from our API. <a class="data" href="/lab/acme/api/reviews">reviews.json</a></p>'
                         f'</main>{decoys()}'))
    if path == "/lab/acme/products":  # THE dataset: a paginated product listing
        pageno = int((query.get("page") or ["1"])[0])
        pages = (len(ACME) + ACME_PER - 1) // ACME_PER
        if pageno < 1 or pageno > pages:
            return html(page("Not found", f"{_acme_nav()}<h1>No such page</h1>"), status=404)
        start = (pageno - 1) * ACME_PER
        cards = "".join(
            f'<article class="product"><span class="name">{n}</span> <span class="price">${p}</span>'
            f'<a class="detail" href="/lab/acme/products/{start + i + 1}">details</a>'
            f'<a class="data" href="/lab/acme/api/products/{start + i + 1}">json</a>'
            + ('<span class="sold-out">Sold out</span>' if sold else '') + '</article>'
            for i, (n, p, sold) in enumerate(ACME[start:start + ACME_PER])
        )
        nxt = f'<a rel="next" href="/lab/acme/products?page={pageno + 1}">next</a>' if pageno < pages else ""
        link = {"Link": f'</lab/acme/products?page={pageno + 1}>; rel="next"'} if pageno < pages else {}
        # a "Related" side panel of FULL record-shaped decoys (article.product) -- so a bare
        # select_all("article.product") also grabs these; the query must SCOPE to section.catalogue.
        related = "".join(f'<article class="product"><span class="name">You may also like {n}</span>'
                          f'<span class="price">$—</span></article>' for n, _p, _s in ACME[:2])
        return html(page(f"Products p{pageno}", f'{_acme_nav()}<main><h1>Shop all</h1>'
                         f'<section class="catalogue">{cards}</section>'
                         f'<nav class="pagination">{nxt}</nav></main>{decoys(related=related)}'), headers=link)
    # the LANDING page: nav decoys + a FEATURED teaser (a 2-item SUBSET) + a link to the full listing
    feat = "".join(f'<article class="teaser"><span class="name">{n}</span></article>' for n, _p, _s in ACME[:2])
    return html(page("Acme Coffee", f'{_acme_nav()}<main><h1>Acme Coffee</h1>'
                     f'<section class="featured"><h2>Featured</h2>{feat}</section>'
                     f'<p><a href="/lab/acme/products">Shop all products →</a></p></main>{decoys()}'))


# --------------------------------------------------------------------------- #
# focused SCENARIO fixtures -- each reproduces ONE onboarding bug through the whole pipeline
# --------------------------------------------------------------------------- #

WALLED = [("Alpha", "10"), ("Bravo", "20"), ("Charlie", "30")]


@fixture("walled", "A browser-only site: a non-browser UA is blocked (403), a browser gets 200",
         "transport:browser_only", browser=True,
         expected={"record_selector": "li.row", "records": len(WALLED), "static_status": 403})
def _walled(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    """A site that BLOCKS a plain (non-browser) fetch with a 403 -- as Wikipedia does to a bare UA --
    but serves the dataset to a real browser. ``browser="auto"`` escalates a blocking 403 to the browser
    (the shared resolve ladder), so a plain fetch AND the crawl reach it; onboarding then bakes the
    browser tier into the blob (from the settled ``final_tier``) so the shipped query re-fetches with it."""
    ua = (headers.get("user-agent") or headers.get("User-Agent") or "").lower()
    if "chrome" not in ua and "firefox" not in ua:  # a static/httpx UA -> blocked
        return html(page("Blocked", "<main><h1>403 — access denied</h1><p>Enable JavaScript.</p></main>"), status=403)
    rows = "".join(f'<li class="row"><span class="name">{n}</span> <span class="qty">{q}</span></li>' for n, q in WALLED)
    return html(page("Inventory", f'<main><h1>Inventory</h1><ul>{rows}</ul></main>{decoys()}'))


RANKING = [("China", "1412"), ("India", "1409"), ("United States", "339")]


@fixture("ranking", "A ranked table with a leading TOTAL row (a required field is absent on it)",
         "extract:total_row",
         expected={"record_selector": "table.rank tbody tr", "rows_total": len(RANKING) + 1,
                   "data_rows": len(RANKING), "total_row_label": "World", "link_selector": "td a"})
def _ranking(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    """A ranked table whose first ``tbody`` row is a WORLD/TOTAL summary that -- unlike the data rows --
    has NO link. A query that reads the name with a non-optional ``select("td a")`` RAISES on that row
    and zeroes the whole result; the fix guides the model to make the field optional (the row yields
    null there). The data rows link to per-country detail pages."""
    head = "<tr><th>Rank</th><th>Country</th><th>Population</th></tr>"
    total = '<tr class="total"><td>—</td><td>World</td><td>3160</td></tr>'  # NO link -> the trap
    body_rows = "".join(
        f'<tr><td>{i + 1}</td><td><a href="/lab/ranking/{i + 1}">{n}</a></td><td>{p}</td></tr>'
        for i, (n, p) in enumerate(RANKING)
    )
    return html(page("Population", f'<main><h1>Countries by population</h1>'
                     f'<table class="rank"><thead>{head}</thead><tbody>{total}{body_rows}</tbody></table></main>'
                     f'{decoys()}'))


FROZEN = ["Row A", "Row B", "Row C"]


@fixture("frozen", "A listing whose ?page= param is IGNORED (every page re-serves the same rows)",
         "pagination:ignored_param",
         expected={"record_selector": "article.item", "records": len(FROZEN)})
def _frozen(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    """A page that ADVERTISES pagination (a rel=next link) but IGNORES ``?page=`` -- every page returns
    the SAME records. The page number is echoed in a canonical link, so a content-hash repeat check is
    fooled (each page's bytes differ) while the RECORDS are identical. Passing the record selector to
    paginate catches the repeat and stops, so the shipped query does not emit duplicate rows."""
    pageno = (query.get("page") or ["1"])[0]
    rows = "".join(f'<article class="item"><span class="name">{n}</span></article>' for n in FROZEN)
    nxt = f'<a rel="next" href="/lab/frozen?page={int(pageno) + 1}">next</a>'  # always offers a next page
    head = f'<link rel="canonical" href="/lab/frozen?page={pageno}">'  # echoes the page -> bytes differ
    return html(page("Frozen list", f'<main><h1>Listing</h1>{rows}'
                     f'<nav class="pagination">{nxt}</nav></main>{decoys()}', head=head))


#: the visible DOM shows only a few TEASER cards, but a JSON-LD island in the page holds the WHOLE
#: dataset -- a query over the visible cards is a silent SUBSET.
_TWOFACE = [("Aeropress", "39"), ("Grinder", "129"), ("Kettle", "59"), ("Scale", "49"),
            ("Filters", "12"), ("Carafe", "35"), ("Tamper", "22"), ("Funnel", "9"),
            ("Jug", "18"), ("Timer", "27"), ("Brush", "7"), ("Beans", "15")]  # 12 in the island, 3 shown


@fixture("twoface", "A few DOM teasers, but a JSON-LD island holds the WHOLE dataset", "extract:json_island",
         expected={"teaser_selector": "article.card", "shown": 3, "total": len(_TWOFACE),
                   "island_selector": "script[type=\"application/ld+json\"]", "array_path": "itemListElement"})
def _twoface(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    """The Next.js / schema.org shape: the DOM renders only 3 FEATURED cards, but a
    ``<script type="application/ld+json">`` ItemList carries all 12 products. A query over the visible
    ``article.card`` extracts 3 rows and looks 'complete'; the whole dataset is in the island
    (``select the script -> .as_json() -> select_all('itemListElement')``). Both carry name + price."""
    island = json.dumps({"@context": "https://schema.org", "@type": "ItemList",
                         "itemListElement": [{"@type": "Product", "name": n, "offers": {"price": p}}
                                             for n, p in _TWOFACE]})
    cards = "".join(f'<article class="card"><span class="name">{n}</span> <span class="price">${p}</span></article>'
                    for n, p in _TWOFACE[:3])  # only the first 3 are rendered
    head = f'<script type="application/ld+json">{island}</script>'
    return html(page("Shop", f'<main><h1>Featured</h1><section class="featured">{cards}</section>'
                     f'<p>See all {len(_TWOFACE)} products in the app.</p></main>{decoys()}', head=head))


#: overlapping page windows: page N shows items [2N-1 .. 2N+2] so consecutive pages SHARE two items,
#: and a fixed "Sponsored" record rides on EVERY page -- so the flat union has duplicate records that
#: only a per-record dedup removes.
_OVERLAP_ITEMS = ["Item 1", "Item 2", "Item 3", "Item 4", "Item 5", "Item 6", "Item 7", "Item 8"]
_OVERLAP_PAGES = 3


@fixture("overlap", "Overlapping pages + a sticky sponsored row (cross-page duplicates)",
         "pagination:overlap",
         expected={"record_selector": "li.item", "pages": _OVERLAP_PAGES, "unique": len(_OVERLAP_ITEMS) + 1,
                   "sticky": "Sponsored"})
def _overlap(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    """Pagination whose page WINDOWS OVERLAP (consecutive pages share two items) AND that repeats a
    STICKY 'Sponsored' record on every page. Page-level dedup can't fix this -- the pages are all
    distinct -- so the flat union has duplicate records; only a per-record ``distinct`` on the project
    yields each record once. Unique = the 8 items + the 1 sponsored row."""
    pageno = int((query.get("page") or ["1"])[0])
    if pageno < 1 or pageno > _OVERLAP_PAGES:
        return html(page("Not found", "<h1>No such page</h1>"), status=404)
    start = (pageno - 1) * 2  # windows overlap by 2: page1=items0..3, page2=items2..5, page3=items4..7
    window = _OVERLAP_ITEMS[start:start + 4]
    sticky = '<li class="item sponsored"><span class="name">Sponsored</span></li>'  # on EVERY page
    rows = sticky + "".join(f'<li class="item"><span class="name">{n}</span></li>' for n in window)
    nxt = f'<a rel="next" href="/lab/overlap?page={pageno + 1}">next</a>' if pageno < _OVERLAP_PAGES else ""
    link = {"Link": f'</lab/overlap?page={pageno + 1}>; rel="next"'} if pageno < _OVERLAP_PAGES else {}
    return html(page(f"Overlap p{pageno}", f'<main><h1>Feed</h1><ul class="feed">{rows}</ul>'
                     f'<nav class="pagination">{nxt}</nav></main>{decoys()}'), headers=link)


#: a category table where the category cell SPANS its items (rowspan) -- item rows omit the category.
_MERGED = [("Fruit", [("Apple", "1"), ("Pear", "2")]), ("Veg", [("Kale", "3"), ("Leek", "4"), ("Yam", "5")])]


@fixture("merged", "A table with a rowspan category cell (merged cells)", "extract:rowspan",
         expected={"header": ["Category", "Item", "Price"], "rows": sum(len(v) for _, v in _MERGED),
                   "first_category": "Fruit"})
def _merged(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    """A category -> items table where the CATEGORY cell uses ``rowspan`` and the item rows below it
    omit it. A per-row query mis-assigns the category (or leaves it null on the spanned rows); the
    fix is ``.table()``, which expands the rowspan so every item row carries its category."""
    head = "<tr><th>Category</th><th>Item</th><th>Price</th></tr>"
    body_rows = ""
    for cat, items in _MERGED:
        for i, (name, price) in enumerate(items):
            cell = f'<td rowspan="{len(items)}">{cat}</td>' if i == 0 else ""  # only the first row has it
            body_rows += f'<tr>{cell}<td>{name}</td><td>${price}</td></tr>'
    return html(page("Catalogue", f'<main><h1>Catalogue</h1>'
                     f'<table class="catalogue"><thead>{head}</thead><tbody>{body_rows}</tbody></table></main>'
                     f'{decoys()}'))


#: a feature-comparison matrix: the PLANS are the columns, the features are the rows (a transposed
#: layout -- the records run across, not down).
_PIVOT_HEAD = ["Plan", "Starter", "Pro", "Enterprise"]
_PIVOT_ROWS = [("Price", ["$9", "$29", "$99"]), ("Users", ["5", "50", "Unlimited"]),
               ("Storage", ["1GB", "10GB", "1TB"])]


@fixture("pivot", "A transposed feature-comparison table (records are COLUMNS)", "extract:transpose",
         expected={"plans": ["Starter", "Pro", "Enterprise"], "features": ["Price", "Users", "Storage"],
                   "starter_price": "$9"})
def _pivot(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    """A pricing/feature matrix laid out TRANSPOSED: each PLAN is a column and each FEATURE is a row, so
    the records run ACROSS. A normal per-row query extracts features (Price/Users) as records -- the
    WRONG axis. ``.table(transpose=True)`` reads each column as a record keyed by the first column."""
    head = "<tr>" + "".join(f"<th>{h}</th>" for h in _PIVOT_HEAD) + "</tr>"
    rows = "".join("<tr><td>" + feat + "</td>" + "".join(f"<td>{v}</td>" for v in vals) + "</tr>"
                   for feat, vals in _PIVOT_ROWS)
    return html(page("Pricing", f'<main><h1>Compare plans</h1>'
                     f'<table class="compare"><thead>{head}</thead><tbody>{rows}</tbody></table></main>'
                     f'{decoys()}'))


LOOP_PAGES = 3
LOOP_PER = 4


@fixture("looppager", "A ?page_num= listing whose '»' Next link LOOPS back to page one", "pagination:loop_next",
         expected={"record_selector": "article.row", "pages": LOOP_PAGES, "per_page": LOOP_PER,
                   "total": LOOP_PAGES * LOOP_PER, "param": "page_num"})
def _looppager(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    """Rebuilt from scrapethissite's hockey table: pagination is by ``?page_num=`` (a param that used
    to be UNRECOGNISED -- ``pagenum`` was, ``page_num`` was not), and the ``»`` Next link on every
    page points BACK to ``page_num=1`` (a broken/self-referential next). So the top-ranked ``next``
    pager LOOPS (repeats page one -> not confirmed) and the pipeline must FALL BACK to the working
    ``page_num`` param pager to walk the whole dataset. Numbered page links expose the param."""
    pageno = int((query.get("page_num") or ["1"])[0])
    if pageno < 1 or pageno > LOOP_PAGES:
        return html(page("Not found", "<h1>No such page</h1>"), status=404)
    start = (pageno - 1) * LOOP_PER
    rows = "".join(f'<article class="row"><span class="name">Item {start + i + 1}</span></article>'
                   for i in range(LOOP_PER))
    nums = "".join(f'<a href="/lab/looppager?page_num={p}">{p}</a> ' for p in range(1, LOOP_PAGES + 1))
    # the '»' Next link ALWAYS points at page 1 -- the broken pager the walk must NOT follow
    loop_next = '<a href="/lab/looppager?page_num=1" aria-label="Next">»</a>'
    return html(page(f"Loop p{pageno}", f'<main><h1>Records</h1>{rows}'
                     f'<nav class="pagination">{nums}{loop_next}</nav></main>{decoys()}'))


# --------------------------------------------------------------------------- #
# JS-gated SPA (auto escalation), XHR-backed feed (SPA + data API)
# --------------------------------------------------------------------------- #

@fixture("spa", "A JS-gated SPA: an empty shell a script fills", "signals:spa", browser=True,
         expected={"static_flags": ["spa"], "remedy": "browser", "records_static": 0, "records_rendered": 3,
                   "record_selector": "li.item", "tiers_auto": ["static", "browser"]})
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
         expected={"api": "/lab/feed/api/items", "records_rendered": 3, "record_selector": "li.item",
                   "xhr_endpoints": ["/lab/feed/api/items"], "items": FEED_ITEMS})
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


JOBS = [{"id": i, "title": t, "location": loc, "content": f"<p>{t}: EMBED-DESCRIPTION-{i}. You will build things.</p>"}
        for i, (t, loc) in enumerate([("Desk Quant Analyst", "London"), ("Data Engineer", "Madrid"),
                                      ("Platform Engineer", "Montreal"), ("Risk Analyst", "Singapore")], 1)]


@fixture("jobs", "A job board: a listing rendered from a JSON API, each posting in a cross-origin embed", "network:embed",
         browser=True, expected={"api": "/lab/jobs/api", "records_rendered": len(JOBS), "record_selector": "li.job",
                                 "jobs": JOBS})
def _jobs(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    """The shape of a careers site on a hosted job board: the listing's records come from the board's
    API (what an agent should read instead), and each detail page shows its posting in an iframe
    served from ANOTHER origin (this server as 127.0.0.1 vs localhost), rendered late by script."""
    if path == "/lab/jobs/api":
        return as_json({"jobs": JOBS, "meta": {"total": len(JOBS)}})
    if path == "/lab/jobs/embed":
        job = next((j for j in JOBS if str(j["id"]) == (query.get("id") or [""])[0]), JOBS[0])
        return html(page("Apply", f"""<div id="post">loading…</div><script>
  setTimeout(function(){{ document.getElementById('post').innerHTML = {json.dumps('<h2>' + str(job['title']) + '</h2>' + str(job['content']))}; }}, 300);
</script>"""))
    if path == "/lab/jobs/detail":
        jid = (query.get("id") or ["1"])[0]
        return html(page("Opening", f"""<main><h1 class="title">…</h1><div id="embed"></div></main><script>
  fetch('/lab/jobs/api').then(function(r){{ return r.json(); }}).then(function(d){{
    var j = d.jobs.filter(function(x){{ return String(x.id) === '{jid}'; }})[0];
    document.querySelector('h1.title').textContent = j.title;
    var other = location.hostname === '127.0.0.1' ? 'localhost' : '127.0.0.1';
    var f = document.createElement('iframe');
    f.src = location.protocol + '//' + other + ':' + location.port + '/lab/jobs/embed?id={jid}';
    f.style.width = '600px'; f.style.height = '300px';
    document.getElementById('embed').appendChild(f);
  }});
</script>"""))
    return html(page("Careers", """<main><h1>Open opportunities</h1><ul id="list"></ul></main><script>
  fetch('/lab/jobs/api').then(function(r){ return r.json(); }).then(function(d){
    document.getElementById('list').innerHTML = d.jobs.map(function(j){
      return '<li class="job"><a href="/lab/jobs/detail?id=' + j.id + '">' + j.title + '</a><p class="loc">' + j.location + '</p></li>';
    }).join('');
  });
</script>"""))


@fixture("loadmore", "An interacted pager: a 'Load more' button appends items via JS", "pagination:interacted",
         browser=True, expected={"record_selector": "li.item", "initial": 3, "total": 12})
def _loadmore(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    return html(page("Load more", """
<main><ul id="list">
  <li class="item">Item 1</li><li class="item">Item 2</li><li class="item">Item 3</li>
</ul>
<button id="more" onclick="loadMore()">Load more</button></main>
<script>
  var loaded = 3, total = 12;
  function loadMore() {
    var list = document.getElementById('list');
    for (var i = 0; i < 3 && loaded < total; i++) {
      loaded++;
      var li = document.createElement('li'); li.className = 'item'; li.textContent = 'Item ' + loaded;
      list.appendChild(li);
    }
    if (loaded >= total) { document.getElementById('more').remove(); }
  }
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


DEEP_PAGES = 3
DEEP_PER_PAGE = 4


@fixture("deep", "Pagination + a per-record 2nd-level resolve (detail page holds a field)", "extract:paginate_resolve",
         expected={"pages": DEEP_PAGES, "per_page": DEEP_PER_PAGE, "total": DEEP_PAGES * DEEP_PER_PAGE,
                   "record_selector": "article.item", "detail_link": "a.more", "sku_of_1": "SKU-1",
                   "flags": ["pagination"]})
def _deep(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    """The DEEPEST shape: the dataset spans PAGES, and a required field (SKU) is not on the listing at
    all -- it lives on each item's own detail page. A correct query must BOTH walk the pagination AND,
    per record, follow the item's link and ``.resolve()`` it to read the field. Detail pages are HTML
    (``span.sku``); the listing rows carry only name + the detail link."""
    if path.startswith("/lab/deep/api/"):  # a JSON detail page (drill into the nested key after resolve)
        k = int(path.rsplit("/", 1)[1])
        return as_json({"id": k, "sku": f"SKU-{k}", "stock": {"count": 5 * k}})
    if path.startswith("/lab/deep/item/"):
        k = int(path.rsplit("/", 1)[1])
        return html(page(f"Item {k}", f'<main><h1>Item {k}</h1>'
                         f'<span class="sku">SKU-{k}</span> <span class="weight">{k}00g</span></main>'))
    pageno = int((query.get("page") or ["1"])[0])
    if pageno < 1 or pageno > DEEP_PAGES:
        return html(page("Not found", "<h1>No such page</h1>"), status=404)
    start = (pageno - 1) * DEEP_PER_PAGE
    items = "".join(
        f'<article class="item"><span class="name">Item {start + i + 1}</span>'
        f'<a class="more" href="/lab/deep/item/{start + i + 1}">details</a>'
        f'<a class="data" href="/lab/deep/api/{start + i + 1}">data</a></article>'
        for i in range(DEEP_PER_PAGE)
    )
    nxt = f'<a rel="next" href="/lab/deep?page={pageno + 1}">next</a>' if pageno < DEEP_PAGES else ""
    link = {"Link": f'</lab/deep?page={pageno + 1}>; rel="next"'} if pageno < DEEP_PAGES else {}
    return html(page(f"Catalog p{pageno}", f'<main><h1>Catalog</h1>{items}'
                     f'<nav class="pagination">{nxt}</nav></main>{decoys()}'), headers=link)


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
         expected={"flags": ["tabbed"], "tabs": ["Upcoming", "Past"], "records": 4, "record_selector": "li.event"})
def _tabs(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    return html(page("Events", """
<main>
<div role="tablist"><button role="tab" aria-selected="true">Upcoming</button><button role="tab">Past</button></div>
<div role="tabpanel"><ul><li class="event">Roast Day</li><li class="event">Cupping</li></ul></div>
<div role="tabpanel" hidden><ul><li class="event">Launch</li><li class="event">Harvest</li></ul></div>
</main>"""))


@fixture("shadow", "Records inside a shadow root", "signals:shadow_dom", browser=True,
         expected={"flags_static": ["shadow_dom"], "records_rendered": 2, "record_selector": "li.p"})
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
         expected={"flags_static": ["iframe"], "records_rendered": 2, "inner": "/lab/iframe/inner", "record_selector": "li.q"})
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
         expected={"flags": ["login_present", "login_required", "forms", "buttons"], "auto_error": "fetch.login_required",
                   "account": "/lab/login", "cookie": "sid=abc123"})
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
         expected={"base": "/lab/errors", "codes": {"404": "fetch.http_status", "500": "fetch.http_status", "503": "fetch.http_status"},
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
         expected={"sitemap_urls": SITE_PAGES, "disallow": ["/lab/login", "/lab/private"], "sitemap_in_robots": True,
                   "seed": "/lab/shop", "must_reach": "/lab/about", "must_skip": "/lab/login", "crawl_pages": 6})
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
         expected={"controls": ["#qty", "#add", "#load"], "rows": "#cart li", "after_add_rows": 1, "after_load_rows": 3,
                   "api": "/lab/app/api/items"})
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
         expected={"initial_rows": 5, "after_scroll_rows": 10, "row_selector": "li.r"})
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


# --------------------------------------------------------------------------- #
# a consent wall (cookie_banner signal -> browser remedy)
# --------------------------------------------------------------------------- #

@fixture("consent", "A consent wall over the content (a CMP banner)", "signals:cookie_banner", browser=True,
         expected={"flags_static": ["cookie_banner"], "vendor": "onetrust", "remedy": "browser",
                   "record_selector": "div.card", "records": 3})
def _consent(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    """The content is there in the served HTML, but a consent-management platform's banner sits
    over it (and blocks scrolling on real sites). The served markup carries the CMP's markers
    (here OneTrust's), so the ``cookie_banner`` signal fires at the static tier and the remedy is
    a browser render whose ``wc.cookies`` page script answers the banner before the snapshot."""
    return html(page("Roasters Coffee", f"""
<script src="https://cdn.cookielaw.org/scripttemplates/otSDKStub.js"></script>
<main><h1>Featured</h1>{_cards()}</main>
<div id="onetrust-banner-sdk" class="lab-notice-bar" role="dialog" aria-label="Privacy">
  <span>We use cookies to make the roast just right.</span>
  <button id="onetrust-reject-all-handler" type="button">Reject all</button>
  <button id="onetrust-accept-btn-handler" class="btn" type="button">Accept all</button>
</div>"""))


# --------------------------------------------------------------------------- #
# a dataset split across differently-shaped sections (the split-query case)
# --------------------------------------------------------------------------- #

ARCHIVE = [("2026-08-30", "Ethiopia Cupping"), ("2026-08-16", "Roast Day"), ("2026-08-02", "Latte Art Jam")]


@fixture("sections", "One dataset split across differently-shaped sections", "extract:split",
         expected={"upcoming_selector": "div.callout", "archived_selector": "li.past", "upcoming": 1,
                   "archived": len(ARCHIVE), "total": 1 + len(ARCHIVE), "upcoming_title": "Autumn Cupping"})
def _sections(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    """A single logical dataset (events) whose rows live in two DIFFERENTLY-SHAPED sections: one
    UPCOMING callout (a single row in its own format) and an ARCHIVED list (many uniform rows).
    A single ``select_all`` tuned to the archive silently drops the upcoming row -- the case the
    pipeline handles by authoring one simple query per section and concatenating the results."""
    past = "".join(f'<li class="past"><span class="date">{d}</span> <span class="what">{w}</span></li>' for d, w in ARCHIVE)
    return html(page("Events", f"""
<main><h1>Events</h1>
<section class="upcoming"><h2>Next up</h2>
  <div class="callout"><span class="when">Oct 3</span> — <span class="what">Autumn Cupping</span>
  <span class="lab-badge">upcoming</span></div>
</section>
<section class="archive"><h2>Past events</h2><ul>{past}</ul></section>
</main>"""))


# --------------------------------------------------------------------------- #
# a real HTML table (rows/columns; GFM markdown), structured metadata (JSON-LD + OpenGraph)
# --------------------------------------------------------------------------- #

TABLE_ROWS = [("Aeropress", "$39", "12"), ("Grinder", "$129", "4"), ("Gooseneck Kettle", "$59", "9")]


@fixture("table", "A data table (rows, columns; renders to GFM markdown)", "extract:table",
         expected={"row_selector": "tbody tr", "rows": len(TABLE_ROWS), "columns": ["Item", "Price", "Stock"],
                   "first_row": list(TABLE_ROWS[0])})
def _table(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    head = "".join(f"<th>{h}</th>" for h in ("Item", "Price", "Stock"))
    rows = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in TABLE_ROWS)
    return html(page("Prices", f"""
<main><h1>Prices</h1><table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></main>"""))


@fixture("structured", "Structured metadata: JSON-LD + Open Graph + canonical", "metadata:structured",
         expected={"schema_types": ["Product"], "og_keys": ["og:image", "og:title", "og:type"],
                   "page_type": "Product", "canonical": "/lab/structured", "feed": "/lab/rss"})
def _structured(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    ld = json.dumps({"@context": "https://schema.org", "@type": "Product", "name": "Aeropress",
                     "offers": {"@type": "Offer", "price": "39.00", "priceCurrency": "USD"}})
    extra = ('<link rel="canonical" href="/lab/structured">'
             '<meta property="og:title" content="Aeropress">'
             '<meta property="og:type" content="Product">'
             '<meta property="og:image" content="/lab/og.png">'
             '<link rel="alternate" type="application/rss+xml" href="/lab/rss">'
             f'<script type="application/ld+json">{ld}</script>')
    return html(page("Aeropress — Roasters", '<main><h1 class="name">Aeropress</h1>'
                     '<p class="price">$39.00</p></main>', head=extra))


# --------------------------------------------------------------------------- #
# resiliency: a rate limit (429 + Retry-After); auth: a bearer-token API
# --------------------------------------------------------------------------- #

@fixture("ratelimit", "A rate limit: 429 with Retry-After (retriable)", "resiliency:rate_limit",
         expected={"status": 429, "retry_after": 2, "retriable": True, "error": "fetch.http_status"})
def _ratelimit(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    return html(page("Too many requests", "<main><h1>429</h1><p>Slow down.</p></main>"),
                status=429, headers={"Retry-After": "2"})


TOKEN = "lab-secret"  # noqa: S105 -- a fixture credential, not a real one


@fixture("token", "A bearer-token JSON API (401 without the header)", "auth:token",
         expected={"header": "Authorization", "scheme": "Bearer", "token": TOKEN, "data": "/lab/token/data",
                   "unauth_status": 401, "items": 3})
def _token(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    if path == "/lab/token/data":
        auth = headers.get("Authorization") or headers.get("authorization") or ""
        if auth != f"Bearer {TOKEN}":
            return as_json({"error": "unauthorized"}, status=401,
                           headers={"WWW-Authenticate": 'Bearer realm="lab"'})
        return as_json({"items": [{"id": i, "name": n} for n, _, i in PRODUCTS]})
    return html(page("API", """
<main><h1>Products API</h1>
<p>Fetch <code>/lab/token/data</code> with <code>Authorization: Bearer lab-secret</code>.</p>
</main>"""))


# --------------------------------------------------------------------------- #
# a listing whose ORDER, FILTERS and LIVENESS the pagination loop must reason about
# --------------------------------------------------------------------------- #

BOARD = [("Senior Engineer", "London"), ("Data Scientist", "Berlin"), ("Product Designer", "Remote"),
         ("Platform Engineer", "Madrid"), ("Staff SRE", "Lisbon")]


@fixture("board", "A live, sorted, filterable listing (ordered/filtered/live)", "signals:pagination_shape",
         expected={"record_selector": "li.post", "records": len(BOARD),
                   "present": ["ordered", "filtered", "live"], "order_direction": "desc"})
def _board(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    """The shape a pagination loop must reason about: the listing is DATE-SORTED newest-first
    (``ordered``, so an early recency stop is sound), it is TIMELY -- the newest rows are days old
    (``live``, so it drifts while you page) -- and it has SORT + FACET controls (``ordered`` /
    ``filtered``, so the order and the active filters are knowable). Dates are generated relative
    to today so the ``live`` window never ages out."""
    today = date.today()
    posts = "".join(
        f'<li class="post"><span class="title">{t}</span> <span class="loc">{loc}</span>'
        f'<time datetime="{(today - timedelta(days=3 * i)).isoformat()}">{3 * i}d ago</time></li>'
        for i, (t, loc) in enumerate(BOARD)  # i=0 newest -> dates run desc (newest-first)
    )
    return html(page("Market", f"""
<main><h1>Market</h1>
<form class="filters" role="search" action="/lab/board" method="get">
  <input type="search" name="q" placeholder="search listings">
  <select name="sort"><option>Newest</option><option>Oldest</option><option>Title</option></select>
  <select name="filter_city"><option>Any city</option><option>London</option><option>Berlin</option></select>
  <label><input type="checkbox" name="remote"> Remote only</label>
  <button type="submit">Apply</button>
</form>
<ul class="listings">{posts}</ul></main>"""))


# --------------------------------------------------------------------------- #
# a two-sibling-row record (rebuilt from the LIVE Hacker News front page): each logical
# story is SPLIT across two adjacent <tr> rows with no wrapper -- the title/site in a
# `tr.athing`, then the points/user/age in the very NEXT unwrapped <tr class="subtext">.
# The record's own subtree (the athing row) does NOT contain points/user/age; reaching them
# needs a following-sibling hop (XPath `./following-sibling::tr[1]//...`). The real timestamp
# lives in a `title=` ATTRIBUTE, not the visible "N hours ago" text.
# --------------------------------------------------------------------------- #

NEWS = [  # (title, domain, points, user, hours-ago)
    ("A tiny CPU emulator written over a weekend", "github.com", 412, "hexdump", 1),
    ("Why we moved our fleet off Kubernetes", "eng.example.com", 388, "sre_anna", 3),
    ("The surprising math of coffee extraction", "brewnotes.io", 274, "roaster", 5),
    ("Show HN: a spreadsheet that runs SQL", "gridql.dev", 201, "cellsmith", 8),
    ("An oral history of the 1kB demo scene", "demozoo.org", 165, "scener", 12),
    ("Ask HN: what killed your side project?", "", 143, "builder", 20),
]


@fixture("news", "A ranked news list where each record spans TWO sibling <tr> rows", "extract:sibling_row",
         expected={"record_selector": "tr.athing", "subtext_hop": "./following-sibling::tr[1]",
                   "records": len(NEWS), "fields": ["title", "points", "user", "age"],
                   "timestamp_attr": "title", "order_direction": "desc"})
def _news(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    """Rebuilt from the real Hacker News front page: the shape where a single logical record is
    NOT one element but two adjacent sibling rows -- a naive ``select_all('tr.athing')`` captures
    the title yet leaves points/user/age EMPTY, because they live in the following sibling. The
    honest ways to extract it: a following-sibling XPath hop per field, or two section queries the
    pipeline aligns. Dates are relative to today (newest-first) so timeliness stays live; the exact
    time is in the age span's ``title`` attribute, the visible text is only 'N hours ago'."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    rows = []
    for i, (title, domain, points, user, hrs) in enumerate(NEWS):
        iso = (now - timedelta(hours=hrs)).replace(microsecond=0).isoformat()
        sid = 40000 + i
        site = (f'<span class="sitebit comhead"> (<a href="from?site={domain}">'
                f'<span class="sitestr">{domain}</span></a>)</span>') if domain else ""
        rows.append(
            f'<tr class="athing submission" id="{sid}">'
            f'<td align="right" valign="top" class="title"><span class="rank">{i + 1}.</span></td>'
            f'<td valign="top" class="votelinks"><center><a href="vote?id={sid}">'
            f'<div class="votearrow" title="upvote"></div></a></center></td>'
            f'<td class="title"><span class="titleline">'
            f'<a href="item?id={sid}">{title}</a>{site}</span></td></tr>'
            f'<tr class="subtext"><td colspan="2"></td><td class="subtext"><span class="subline">'
            f'<span class="score" id="score_{sid}">{points} points</span> by '
            f'<a href="user?id={user}" class="hnuser">{user}</a> '
            f'<span class="age" title="{iso}"><a href="item?id={sid}">{hrs} hours ago</a></span>'
            f'</span></td></tr>'
        )
    return html(page("Hacker Lab", f"""
<main><h1>Top stories</h1>
<table class="itemlist" border="0" cellpadding="0" cellspacing="0">
<tbody>{''.join(rows)}</tbody></table></main>"""))


# --------------------------------------------------------------------------- #
# a LIST-valued field (rebuilt from quotes.toscrape.com): each record carries a field
# that is MANY values, not one -- a quote's tags. The tags are BOTH a repeating
# `<a class="tag">` list AND mirrored in a `<meta class="keywords" content="a,b,c">`, so a
# field can be captured as a nested list (select_all inside extract) or split from the attr.
# --------------------------------------------------------------------------- #

QUOTES = [  # (text, author, [tags])
    ("The world as we have created it is a process of our thinking.", "Albert Einstein",
     ["change", "deep-thoughts", "thinking", "world"]),
    ("It is our choices that show what we truly are, far more than our abilities.", "J.K. Rowling",
     ["abilities", "choices"]),
    ("There are only two ways to live your life.", "Albert Einstein", ["inspirational", "life", "live", "miracle"]),
    ("A woman is like a tea bag; you never know how strong it is until it's in hot water.", "Eleanor Roosevelt",
     ["misattributed-eleanor-roosevelt"]),
    ("Imperfection is beauty, madness is genius.", "Marilyn Monroe", ["be-yourself", "inspirational"]),
    ("Try not to become a man of success. Rather become a man of value.", "Albert Einstein",
     ["adulthood", "success", "value"]),
]


@fixture("quotes", "Records with a LIST-valued field (a quote's many tags)", "extract:list_field",
         expected={"record_selector": "div.quote", "records": len(QUOTES), "list_field": "tags",
                   "tags_selector": "a.tag", "keywords_attr": "content", "first_tags": QUOTES[0][2]})
def _quotes(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    """Rebuilt from quotes.toscrape.com: a field that is a LIST, not a scalar -- each quote has
    several tags. Captured either as a nested list (``tags=wq.doc.select_all('a.tag').attr('text')``)
    or by splitting the mirrored ``<meta class='keywords' content='a,b,c'>`` attribute. The point is
    that ``extract`` handles a many-valued field, not just one-value-per-column."""
    quotes = "".join(
        f'<div class="quote"><span class="text">“{t}”</span>'
        f'<span>by <small class="author">{a}</small></span>'
        f'<div class="tags">Tags: <meta class="keywords" content="{",".join(tags)}">'
        + "".join(f'<a class="tag" href="/lab/quotes/tag/{g}">{g}</a>' for g in tags)
        + "</div></div>"
        for t, a, tags in QUOTES
    )
    return html(page("Quotes", f'<main><h1>Quotes to scrape</h1>{quotes}</main>'))


# --------------------------------------------------------------------------- #
# a value encoded in a CLASS TOKEN + the full value in a title ATTRIBUTE (rebuilt from
# books.toscrape.com): the star rating is the word in `class="star-rating Three"` (not text,
# not a clean data- attr), and the full product title is in the anchor's `title=` while the
# visible link text is truncated. Both need reading an attribute, then picking a token out of it.
# --------------------------------------------------------------------------- #

CATALOG = [  # (full title, rating word, price)
    ("A Light in the Attic", "Three", "51.77"),
    ("Tipping the Velvet", "One", "53.74"),
    ("Soumission", "One", "50.10"),
    ("Sharp Objects", "Four", "47.82"),
    ("Sapiens: A Brief History of Humankind", "Five", "54.23"),
    ("The Requiem Red", "One", "22.65"),
]


@fixture("catalog", "A value in a CLASS TOKEN + the full title in an attribute", "extract:class_token",
         expected={"record_selector": "article.product_pod", "records": len(CATALOG),
                   "rating_from": "class", "rating_token_of": "star-rating <word>",
                   "title_attr": "title", "price_selector": "p.price_color", "first_rating": CATALOG[0][1]})
def _catalog(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    """Rebuilt from books.toscrape.com: the star rating is encoded as the SECOND class token
    (``class="star-rating Three"`` -- read ``.attr('class')`` and take the word after
    'star-rating'), and the FULL product title lives in the anchor's ``title`` attribute while the
    visible text is truncated with an ellipsis. Both defeat a naive ``.attr('text')``; the value is
    in an attribute, and the rating needs a token pulled out of the class string."""
    pods = "".join(
        f'<article class="product_pod">'
        f'<div class="image_container"><a href="catalogue/book_{i}/index.html">'
        f'<img class="thumbnail" alt="{title}"></a></div>'
        f'<p class="star-rating {rating}"><i class="icon-star"></i><i class="icon-star"></i></p>'
        f'<h3><a href="catalogue/book_{i}/index.html" title="{title}">{title[:20]}{"…" if len(title) > 20 else ""}</a></h3>'
        f'<div class="product_price"><p class="price_color">£{price}</p>'
        f'<p class="instock availability">In stock</p></div></article>'
        for i, (title, rating, price) in enumerate(CATALOG)
    )
    return html(page("Catalog", f'<main><h1>Books</h1><section class="products">{pods}</section></main>'))


# --------------------------------------------------------------------------- #
# a small linked site to crawl (scope, dedup across cross-links / cycles)
# --------------------------------------------------------------------------- #

#: path -> (title, out-links). Cross-links and back-links form cycles (each page is fetched
#: once); one OFF-SITE link is out of scope under same_origin.
SITE: dict[str, tuple[str, list[str]]] = {
    "/lab/site": ("Handbook", ["/lab/site/guides", "/lab/site/api", "/lab/site/about", "https://example.com/external"]),
    "/lab/site/guides": ("Guides", ["/lab/site/guides/1", "/lab/site/guides/2", "/lab/site", "/lab/site/api"]),
    "/lab/site/api": ("API reference", ["/lab/site/guides", "/lab/site"]),
    "/lab/site/about": ("About", ["/lab/site"]),
    "/lab/site/guides/1": ("Quickstart", ["/lab/site/guides", "/lab/site/guides/2"]),
    "/lab/site/guides/2": ("Recipes", ["/lab/site/guides", "/lab/site/guides/1"]),
}


@fixture("site", "A small linked site to crawl (scope, dedup, cycles)", "crawl:site",
         expected={"seed": "/lab/site", "pages": sorted(SITE), "count": len(SITE),
                   "offsite": "https://example.com/external"})
def _site(method: str, path: str, query: Query, headers: dict[str, str], body: bytes) -> Any:
    node = SITE.get(path)
    if node is None:
        return html(page("Not found", "<main><h1>404</h1></main>"), status=404)
    title, links = node
    nav = "".join(f'<li><a href="{u}">{title if u == path else u.rsplit("/", 1)[-1] or "home"}</a></li>' for u in links)
    return html(page(title, f'<main><h1>{title}</h1><p>Part of the handbook.</p>'
                     f'<nav aria-label="site"><ul>{nav}</ul></nav></main>'))
