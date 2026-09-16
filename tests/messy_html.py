"""Generators for MESSY, DEVIOUS HTML + a set of query-authoring scenarios.

Each :class:`Scenario` bundles: the pages to serve, the target schema (brief), the CORRECT
rows we expect out, and a known-good ``solution`` query (a ``wq.doc`` chain) that extracts
them. The deterministic test (``test_messy_html.py``) proves every scenario is SOLVABLE and
the DSL handles it; the LLM eval (``scripts/query_eval.py``) then checks whether the model
finds an equivalent query on its own -- i.e. whether it can write genuinely complex queries
(nested resolves, sibling-no-root, regex extraction) against genuinely tricky content
(empty-vs-archived, an Adobe-style SPA false flag).

The messiness helpers wrap real records in hashed/utility classes, extra nesting, HTML
comments, whitespace and decoy elements -- so a durable selector has to hook on *meaning*
(a semantic class / attribute / structure), not position or a pretty class name.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

_HEX = "0123456789abcdef"


@dataclass
class Scenario:
    name: str
    desc: str                       # the brief's free-text ask
    fields: list[str]               # the target schema (dotted; trailing ? = optional)
    pages: dict[str, str]           # served path -> html; the entry is the first key
    expected: "list[dict] | str"    # the correct rows, or "EMPTY" (0 rows is the right answer)
    solution: str                   # a known-good wq.doc chain that yields `expected`
    entry: str = "/"
    notes: str = ""
    # optional signal assertions (name -> should-be-present), e.g. {"spa": False}
    flags: dict[str, bool] = field(default_factory=dict)
    # served path -> Content-Type (paths not listed default to text/html); lets a page be
    # served as JSON / XML / RSS so the right document kind is sniffed.
    content_types: dict[str, str] = field(default_factory=dict)
    # the same-origin XHR/fetch data endpoints a browser render of the ENTRY page would
    # observe (e.g. ["/api/posts"]); used to test that the pipeline catches the real data
    # source when the main page is only a shell.
    xhr_endpoints: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# messiness helpers
# --------------------------------------------------------------------------- #

def _rng(seed: int) -> random.Random:
    return random.Random(seed)


def _hash(r: random.Random, n: int = 7) -> str:
    return "".join(r.choice(_HEX) for _ in range(n))


def _cls(r: random.Random, *stable: str) -> str:
    """A class attribute mixing STABLE hooks with hashed/utility noise, order shuffled."""
    util = [f"css-{_hash(r)}", f"mt-{r.randint(0, 8)}", "flex", f"jsx-{r.randint(1000, 9999)}"]
    parts = [*stable, *r.sample(util, k=r.randint(1, len(util)))]
    r.shuffle(parts)
    return " ".join(parts)


def _wrap(r: random.Random, inner: str, depth: int = 2) -> str:
    """Bury ``inner`` under a few noise wrapper divs with hashed classes."""
    for _ in range(depth):
        inner = f'<div class="{_cls(r)}"><!-- {_hash(r)} -->{inner}</div>'
    return inner


def _decoys(r: random.Random) -> str:
    """Chrome that looks like content but isn't: nav, a promo card, a hidden template."""
    return (
        f'<nav class="{_cls(r, "site-nav")}"><a href="/">Home</a><a href="/about">About</a></nav>'
        f'<div class="{_cls(r, "promo")}"><h3>Subscribe to our newsletter</h3>'
        f'<button>Sign up</button></div>'
        f'<template id="card-tmpl"><article class="item"><h2>{{{{title}}}}</h2></article></template>'
        f'<div hidden class="{_cls(r, "skeleton")}"><article class="item"><h2>Loading…</h2></article></div>'
    )


def _page(body: str, *, head: str = "") -> str:
    return (f"<!doctype html><html><head><meta charset=utf-8>{head}</head>"
            f"<body>{body}</body></html>")


# --------------------------------------------------------------------------- #
# 1. flat sibling run, no per-record container, SOME records missing the date
#    (the classic press-release shape + the :scope+p "grabs the wrong sibling" trap)
# --------------------------------------------------------------------------- #

def flat_sibling_press(seed: int = 1) -> Scenario:
    r = _rng(seed)
    items = [
        ("Acme to Acquire Foobar", "/news/2026/09/acme-foobar", "September 12, 2026"),
        ("Acme Reports Record Quarter", "/news/2026/08/acme-q3", "August 28, 2026"),
        ("Acme Launches Widget Cloud", "/news/2026/08/widget-cloud", None),  # NO date line
        ("Acme Partners with Globex", "/news/2026/07/globex", "July 15, 2026"),
    ]
    rows = ""
    for title, href, date in items:
        rows += f'<p><a href="{href}">{title}</a></p>'
        if date is not None:
            rows += f'<p class="pubdate">{date}</p>'   # date is a sibling <p>, NOT nested
        rows += "<hr>"
    body = _decoys(r) + "<main>" + _wrap(r, "<h1>Press Releases</h1>" + rows, depth=2) + "</main>"
    expected = [
        {"title": t, "url": "https://SITE" + h, "date": d}
        for (t, h, d) in items
    ]
    solution = (
        # attribute selectors aren't allowed inside CSS :has(), so the record selector is an
        # XPath matching a <p> that links a news item; the date is the adjacent p.pubdate.
        'wq.doc.select_all(\'//p[a[starts-with(@href, "/news/")]]\')'
        '.extract(title=wq.doc.select("a").attr("text"),'
        ' url=wq.doc.select("a").attr("href"),'
        ' date=wq.doc.select(":scope + p.pubdate", optional=True).attr("text")).project()'
    )
    return Scenario(
        name="flat_sibling_press",
        desc="every press release, each with its title, a link to it, and its publication date",
        fields=["title", "url", "date?"],
        pages={"/": body}, expected=expected, solution=solution,
        notes="records are sibling <p>/<hr> runs with no wrapper; one item has NO date <p> -- a "
              "naive ':scope + p' would grab the next headline, so the date must be scoped to p.pubdate.",
    )


# --------------------------------------------------------------------------- #
# 2. regex extraction: value/unit split out of a text blob + rating from a class
# --------------------------------------------------------------------------- #

def regex_products(seed: int = 2) -> Scenario:
    r = _rng(seed)
    data = [
        ("Aeropress Go", "USD 39.95 / each", "Four"),
        ("Burr Grinder", "USD 1,299.00 / unit", "Five"),
        ("Gooseneck Kettle", "USD 89.00 / each", "Three"),
    ]
    cards = ""
    for name, price, rating in data:
        cards += _wrap(r,
            f'<article class="{_cls(r, "product")}">'
            f'<span class="{_cls(r, "name")}">{name}</span>'
            f'<span class="{_cls(r, "price")}">{price}</span>'
            f'<span class="{_cls(r, "stars")}" data-rating="{rating}"></span></article>', depth=1)
    body = _decoys(r) + "<main>" + cards + "</main>"
    expected = [
        {"name": "Aeropress Go", "price": {"value": "39.95", "unit": "each"}, "rating": "Four"},
        {"name": "Burr Grinder", "price": {"value": "1,299.00", "unit": "unit"}, "rating": "Five"},
        {"name": "Gooseneck Kettle", "price": {"value": "89.00", "unit": "each"}, "rating": "Three"},
    ]
    solution = (
        'wq.doc.select_all("article.product").extract('
        'name=wq.doc.select("[class*=name]").attr("text"),'
        ' price=wq.doc.select("[class*=price]").extract('
        '   value=wq.doc.regex(r"[\\d.,]+"),'
        '   unit=wq.doc.regex(r"/\\s*(\\w+)", group=1)).project(),'
        ' rating=wq.doc.select("[data-rating]").attr("data-rating")).project()'
    )
    return Scenario(
        name="regex_products",
        desc="each product with its name, a structured price (numeric value + unit) and its star rating",
        fields=["name", "price.value", "price.unit", "rating"],
        pages={"/": body}, expected=expected, solution=solution,
        notes="price value+unit must be regex'd out of 'USD 1,299.00 / unit'; the rating word is "
              "only in the class attribute ('star-rating Five').",
    )


# --------------------------------------------------------------------------- #
# 3. DEVIOUS content: empty 'Upcoming' (placeholder text) vs a populated 'Past' section.
#    The right answer for "upcoming events" is ZERO rows -- and the query must NOT grab the
#    archived events, nor count the 'No upcoming events' placeholder as a row.
# --------------------------------------------------------------------------- #

def events_empty_upcoming(seed: int = 3) -> Scenario:
    r = _rng(seed)
    archived = [("Annual Summit 2025", "October 3, 2025"),
                ("Dev Conference 2025", "May 12, 2025"),
                ("Launch Party", "January 20, 2025")]
    arch_html = "".join(
        f'<li class="{_cls(r, "event")}"><span class="{_cls(r, "ev-name")}">{n}</span>'
        f'<time class="{_cls(r, "ev-date")}">{d}</time></li>' for n, d in archived)
    body = _decoys(r) + "<main>" + _wrap(r,
        f'<section id="upcoming"><h2>Upcoming Events</h2>'
        f'<p class="{_cls(r, "empty-state")}">No upcoming events at this time. Check back soon!</p>'
        f'</section>'
        f'<section id="past"><h2>Past Events</h2><ul>{arch_html}</ul></section>', depth=1) + "</main>"
    solution = (
        'wq.doc.select_all("#upcoming li.event").extract('
        'name=wq.doc.select("[class*=ev-name]").attr("text"),'
        ' date=wq.doc.select("time").attr("text")).project()'
    )
    return Scenario(
        name="events_empty_upcoming",
        desc="the UPCOMING events, each with its name and date (only future events, not past ones)",
        fields=["name", "date"],
        pages={"/": body}, expected="EMPTY", solution=solution,
        notes="there are NO upcoming events (only a placeholder) -- the correct output is 0 rows; "
              "the past-events section is a decoy that must not be scraped, and the 'No upcoming "
              "events' text must not be returned as a row.",
    )


def events_some_upcoming(seed: int = 4) -> Scenario:
    r = _rng(seed)
    upcoming = [("Product Webinar", "September 25, 2026"), ("User Group Meetup", "October 8, 2026")]
    archived = [("Annual Summit 2025", "October 3, 2025")]
    up_html = "".join(
        f'<li class="{_cls(r, "event")}"><span class="{_cls(r, "ev-name")}">{n}</span>'
        f'<time class="{_cls(r, "ev-date")}">{d}</time></li>' for n, d in upcoming)
    arch_html = "".join(
        f'<li class="{_cls(r, "event")}"><span class="{_cls(r, "ev-name")}">{n}</span>'
        f'<time class="{_cls(r, "ev-date")}">{d}</time></li>' for n, d in archived)
    body = _decoys(r) + "<main>" + _wrap(r,
        f'<section id="upcoming"><h2>Upcoming Events</h2><ul>{up_html}</ul></section>'
        f'<section id="past"><h2>Past Events</h2><ul>{arch_html}</ul></section>', depth=1) + "</main>"
    expected = [{"name": n, "date": d} for n, d in upcoming]  # ONLY the upcoming ones
    solution = (
        'wq.doc.select_all("#upcoming li.event").extract('
        'name=wq.doc.select("[class*=ev-name]").attr("text"),'
        ' date=wq.doc.select("time").attr("text")).project()'
    )
    return Scenario(
        name="events_some_upcoming",
        desc="the UPCOMING events, each with its name and date (only future events, not past ones)",
        fields=["name", "date"],
        pages={"/": body}, expected=expected, solution=solution,
        notes="must select ONLY the #upcoming section's events, not the identical-looking past ones.",
    )


# --------------------------------------------------------------------------- #
# 4. Adobe-style SPA FALSE FLAG: framework markers + a hydration blob, but the FULL
#    content is already in the served HTML. The pipeline must NOT flag spa (contra
#    evidence), and a plain static query must extract the records.
# --------------------------------------------------------------------------- #

def spa_false_flag(seed: int = 5) -> Scenario:
    r = _rng(seed)
    posts = [("Scaling our edge network", "September 14, 2026"),
             ("A deep dive into our cache", "September 9, 2026"),
             ("Postmortem: the big outage", "September 2, 2026"),
             ("Introducing project Nimbus", "August 27, 2026")]
    cards = "".join(
        _wrap(r, f'<article class="{_cls(r, "post")}"><h2 class="{_cls(r, "post-title")}">{t}</h2>'
                 f'<time class="{_cls(r, "post-date")}">{d}</time>'
                 f'<p>{"Real, server-rendered article body. " * 20}</p></article>', depth=1)
        for t, d in posts)
    # the false flags: a framework root + a serialized state blob + a next-data script
    head = '<script>window.__NEXT_DATA__ = {"props":{"pageProps":{}}}</script>'
    body = (f'<div id="__next" data-reactroot>'
            + _decoys(r) + "<main>" + cards + "</main></div>"
            + '<script src="/_next/static/chunks/main.js"></script>')
    expected = [{"title": t, "date": d} for t, d in posts]
    solution = (
        'wq.doc.select_all("article.post").extract('
        'title=wq.doc.select("[class*=post-title]").attr("text"),'
        ' date=wq.doc.select("time").attr("text")).project()'
    )
    return Scenario(
        name="spa_false_flag",
        desc="every blog post on the page, each with its title and publication date",
        fields=["title", "date"],
        pages={"/": _page(body, head=head)}, expected=expected, solution=solution,
        flags={"spa": False},  # framework markers fire, but static content present -> contra wins
        notes="Adobe-style FALSE FLAG: __NEXT_DATA__ / data-reactroot / _next chunks look like an "
              "SPA, but the full content is server-rendered. spa must be contra'd OFF and the static "
              "query must work -- no needless browser escalation.",
    )


# --------------------------------------------------------------------------- #
# 5. NESTED RESOLVE: a listing where a required field lives only on each item's
#    detail page -- the query must resolve a sub-reference per record.
# --------------------------------------------------------------------------- #

def nested_detail(seed: int = 6) -> Scenario:
    r = _rng(seed)
    items = [("Widget Pro", "/item/wp", "SKU-9910"), ("Cog Deluxe", "/item/cd", "SKU-9911")]
    listing = "".join(
        _wrap(r, f'<li class="{_cls(r, "product")}">'
                 f'<a class="{_cls(r, "detail-link")}" href="{href}">{name}</a></li>', depth=1)
        for name, href, _ in items)
    pages = {"/": _decoys(r) + "<main><ul>" + listing + "</ul></main>"}
    for name, href, sku in items:
        pages[href] = (f'<main><h1 class="{_cls(r, "title")}">{name}</h1>'
                       f'<dl><dt>SKU</dt><dd class="{_cls(r, "sku")}">{sku}</dd></dl></main>')
    expected = [{"name": n, "sku": s} for n, _, s in items]
    solution = (
        'wq.doc.select_all("li.product").extract('
        'name=wq.doc.select("a").attr("text"),'
        ' sku=wq.doc.select("a").attr("href").resolve().select("[class*=sku]").attr("text")).project()'
    )
    return Scenario(
        name="nested_detail",
        desc="each product with its name and its SKU code",
        fields=["name", "sku"],
        pages=pages, expected=expected, solution=solution,
        notes="the SKU is NOT on the listing -- it is only on each product's detail page, so the "
              "query must follow the link and resolve() the detail page per record (nested resolve).",
    )


# --------------------------------------------------------------------------- #
# 6. RSS/XML feed: the data is an XML feed, not HTML. The query must select <item>
#    records and read child elements -- incl. a MIXED-CASE tag (<pubDate>) the CSS
#    element-name match has to reach, and a namespaced field it must NOT confuse.
# --------------------------------------------------------------------------- #

def rss_feed(seed: int = 7) -> Scenario:
    items = [
        ("Q3 earnings released", "https://ir.acme.com/news/q3", "Tue, 09 Sep 2026 13:00:00 GMT"),
        ("Acme acquires Foobar", "https://ir.acme.com/news/foobar", "Fri, 05 Sep 2026 09:30:00 GMT"),
        ("New CFO appointed", "https://ir.acme.com/news/cfo", "Mon, 01 Sep 2026 16:45:00 GMT"),
    ]
    items_xml = "".join(
        f"<item><title>{t}</title><link>{u}</link>"
        f"<guid isPermaLink='true'>{u}</guid>"
        f"<pubDate>{d}</pubDate>"
        f"<description>Full story: {t}.</description></item>"
        for t, u, d in items
    )
    xml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<rss version='2.0' xmlns:atom='http://www.w3.org/2005/Atom'>"
        "<channel><title>Acme Investor News</title>"
        "<link>https://ir.acme.com</link>"
        "<atom:link href='https://ir.acme.com/feed.xml' rel='self'/>"
        "<description>Press releases</description>"
        f"{items_xml}</channel></rss>"
    )
    expected = [{"title": t, "url": u, "date": d} for t, u, d in items]
    solution = (
        'wq.doc.select_all("item").extract('
        'title=wq.doc.select("title").attr("text"),'
        ' url=wq.doc.select("link").attr("text"),'
        ' date=wq.doc.select("pubDate").attr("text")).project()'
    )
    return Scenario(
        name="rss_feed",
        desc="every press release in the feed, each with its title, link and publication date",
        fields=["title", "url", "date"],
        pages={"/feed.xml": xml}, entry="/feed.xml",
        content_types={"/feed.xml": "application/rss+xml; charset=utf-8"},
        expected=expected, solution=solution,
        notes="the source is an RSS/XML feed, not HTML -- records are <item>s; the date lives in a "
              "MIXED-CASE <pubDate> tag and there is a namespaced <atom:link> decoy that must NOT be "
              "picked up as the item link.",
    )


# --------------------------------------------------------------------------- #
# 7. JSON API: the data is a JSON document (an XHR/API response). The query must use
#    dotted-path selection into a nested array, not CSS.
# --------------------------------------------------------------------------- #

def json_api(seed: int = 8) -> Scenario:
    articles = [
        ("Scaling the edge", "/blog/scaling-edge", "2026-09-14"),
        ("Cache internals", "/blog/cache", "2026-09-09"),
        ("The big outage", "/blog/outage", "2026-09-02"),
    ]
    import json as _json
    payload = {
        "meta": {"page": 1, "total": 3},  # decoy fields at the top level
        "data": {
            "articles": [
                {"headline": t, "slug": s, "published_at": d, "author": {"name": "staff"}}
                for t, s, d in articles
            ],
            "featured": {"headline": "PINNED — ignore me", "slug": "/x", "published_at": "2020-01-01"},
        },
    }
    body = _json.dumps(payload)
    expected = [{"title": t, "url": s, "date": d} for t, s, d in articles]
    solution = (
        'wq.doc.select_all("data.articles").extract('
        'title=wq.doc.attr("headline"),'
        ' url=wq.doc.attr("slug"),'
        ' date=wq.doc.attr("published_at")).project()'
    )
    return Scenario(
        name="json_api",
        desc="every article, each with its title, its URL slug and its publication date",
        fields=["title", "url", "date"],
        pages={"/api/articles": body}, entry="/api/articles",
        content_types={"/api/articles": "application/json"},
        expected=expected, solution=solution,
        notes="the source is a JSON API response -- the records are at data.articles[] (dotted-path "
              "select, NOT CSS); meta.* and data.featured are decoys the query must not include.",
    )


# --------------------------------------------------------------------------- #
# 8. XHR-behind-a-shell: the main page is an SPA shell (empty root, spa fires) and the
#    real records arrive by an XHR to a JSON endpoint. The right SOURCE is that endpoint --
#    a render of the shell must SURFACE it (xhr_endpoints), telling apart the data API from
#    the analytics beacon. `xhr_endpoints` lists what a render of "/" observes.
# --------------------------------------------------------------------------- #

def xhr_behind_shell(seed: int = 9) -> Scenario:
    posts = [
        ("Launch week recap", "/p/launch-week", "2026-09-15"),
        ("Under the hood", "/p/under-the-hood", "2026-09-10"),
    ]
    # the served HTML is only a shell: an empty hydration root + a bundle -> spa fires.
    shell = _page(
        f'<div id="__next"></div>'
        f'<script src="/_next/static/chunks/main.js"></script>',
        head='<script>window.__NEXT_DATA__={"props":{}}</script>',
    )
    import json as _json
    api = _json.dumps({"posts": [{"title": t, "path": u, "date": d} for t, u, d in posts]})
    expected = [{"title": t, "url": u, "date": d} for t, u, d in posts]
    # the right source is the JSON endpoint the shell fetches, queried by dotted path.
    solution = (
        'wq.doc.select_all("posts").extract('
        'title=wq.doc.attr("title"),'
        ' url=wq.doc.attr("path"),'
        ' date=wq.doc.attr("date")).project()'
    )
    return Scenario(
        name="xhr_behind_shell",
        desc="every blog post, each with its title, URL and date (data loads over XHR)",
        fields=["title", "url", "date"],
        pages={"/": shell, "/api/posts": api},
        content_types={"/api/posts": "application/json"},
        entry="/api/posts",  # deterministic solution runs against the discovered data source
        expected=expected, solution=solution,
        flags={"spa": True},  # the shell must flag spa so auto renders + finds the XHR
        xhr_endpoints=["/api/posts", "https://www.google-analytics.com/collect"],
        notes="the main page is a SHELL (spa fires); the records come from an XHR to /api/posts "
              "(JSON). The pipeline must render the shell, SURFACE the data endpoint (telling it "
              "apart from the analytics beacon), and query that JSON -- not the empty shell.",
    )


def all_scenarios() -> list[Scenario]:
    return [
        flat_sibling_press(),
        regex_products(),
        events_empty_upcoming(),
        events_some_upcoming(),
        spa_false_flag(),
        nested_detail(),
        rss_feed(),
        json_api(),
        xhr_behind_shell(),
    ]
