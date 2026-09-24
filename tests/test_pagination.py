"""Pagination: ``doc.paginate(...)`` walks a dataset's pages into a Collection[Document], and the
rest of the chain extracts ACROSS every page (the fix for the page-1-only bug). Covers by="link"
(rel=next) and by="param" (?page=N), the max_pages bound, and the out-of-range clamp guard."""

from webclient import WebClient
from webclient.interface import wq


def _page(records, next_url=None):
    items = "".join(f'<article class="r"><span class="n">{n}</span></article>' for n in records)
    nxt = f'<a rel="next" href="{next_url}">Next</a>' if next_url else ""
    return f"<html><body><main>{items}</main>{nxt}</body></html>"


def test_paginate_by_link_returns_all_pages(httpserver):
    httpserver.expect_request("/p1").respond_with_data(_page(["A", "B"], "/p2"), content_type="text/html")
    httpserver.expect_request("/p2").respond_with_data(_page(["C", "D"], "/p3"), content_type="text/html")
    httpserver.expect_request("/p3").respond_with_data(_page(["E"]), content_type="text/html")  # no next
    with WebClient() as wc:
        pages = wc.fetch(httpserver.url_for("/p1")).paginate(by="link", max_pages=10)
    assert len(list(pages)) == 3  # followed rel=next to the end


def test_paginate_by_link_extracts_the_whole_dataset(httpserver):
    # the page-1-only bug, fixed: paginate + the flat-map body -> rows from EVERY page.
    httpserver.expect_request("/p1").respond_with_data(_page(["A", "B"], "/p2"), content_type="text/html")
    httpserver.expect_request("/p2").respond_with_data(_page(["C", "D"], "/p3"), content_type="text/html")
    httpserver.expect_request("/p3").respond_with_data(_page(["E"]), content_type="text/html")
    plan = (
        wq.reference(httpserver.url_for("/p1")).resolve()
        .paginate(by="link", max_pages=10)
        .select_all("article.r").extract(n=wq.doc.select(".n").attr("text")).project()
    )
    rows = plan.collect()
    assert [r["n"] for r in rows] == ["A", "B", "C", "D", "E"]  # flat across all 3 pages


def test_paginate_by_param_walks_until_empty(httpserver):
    httpserver.expect_request("/list", query_string="page=1").respond_with_data(_page(["A", "B"]), content_type="text/html")
    httpserver.expect_request("/list", query_string="page=2").respond_with_data(_page(["C", "D"]), content_type="text/html")
    httpserver.expect_request("/list", query_string="page=3").respond_with_data(_page(["E"]), content_type="text/html")
    httpserver.expect_request("/list", query_string="page=4").respond_with_data("", status=404)
    plan = (
        wq.reference(httpserver.url_for("/list") + "?page=1").resolve()
        .paginate(by="param", name="page", start=1, step=1, max_pages=10)
        .select_all("article.r").extract(n=wq.doc.select(".n").attr("text")).project()
    )
    rows = plan.collect()
    assert [r["n"] for r in rows] == ["A", "B", "C", "D", "E"]  # stopped at the 404 (page 4)


def test_paginate_is_bounded_by_max_pages(httpserver):
    # every page links to the next forever; max_pages caps the walk.
    for i in range(1, 30):
        httpserver.expect_request(f"/n{i}").respond_with_data(_page([f"r{i}"], f"/n{i+1}"), content_type="text/html")
    with WebClient() as wc:
        pages = wc.fetch(httpserver.url_for("/n1")).paginate(by="link", max_pages=5)
    assert len(list(pages)) == 5


def test_paginate_stops_on_a_clamped_repeat(httpserver):
    # a loop/clamp: c2's "next" points back to c1 (whose content is identical to the page already
    # seen) -> the walk stops rather than cycling c1<->c2 forever.
    httpserver.expect_request("/c1").respond_with_data(_page(["A"], "/c2"), content_type="text/html")
    httpserver.expect_request("/c2").respond_with_data(_page(["B"], "/c1"), content_type="text/html")  # loops to c1
    with WebClient() as wc:
        pages = list(wc.fetch(httpserver.url_for("/c1")).paginate(by="link", max_pages=50))
    # c1, c2, then c2's next re-serves c1 (a repeat) -> stop; the repeat is not appended
    assert len(pages) == 2


# -- by="cursor": a keyset token read off each page -------------------------------------------

def _cursor_page(records, cursor=None):
    items = "".join(f'<article class="r"><span class="n">{n}</span></article>' for n in records)
    more = f'<a class="more" data-cursor="{cursor}">More</a>' if cursor else ""
    return f"<html><body><main>{items}</main>{more}</body></html>"


def test_paginate_by_cursor_follows_a_keyset_token(httpserver):
    # each page carries the NEXT page's cursor in an attribute (not a rel=next link); by="cursor"
    # reads it and puts it in ?cursor=<token>. The last page has no token -> stop.
    httpserver.expect_request("/feed", query_string="").respond_with_data(_cursor_page(["A", "B"], "k2"), content_type="text/html")
    httpserver.expect_request("/feed", query_string="cursor=k2").respond_with_data(_cursor_page(["C", "D"], "k3"), content_type="text/html")
    httpserver.expect_request("/feed", query_string="cursor=k3").respond_with_data(_cursor_page(["E"]), content_type="text/html")  # no cursor
    plan = (
        wq.reference(httpserver.url_for("/feed")).resolve()
        .paginate(by="cursor", cursor="a.more", cursor_attr="data-cursor", name="cursor", max_pages=10)
        .select_all("article.r").extract(n=wq.doc.select(".n").attr("text")).project()
    )
    assert [r["n"] for r in plan.collect()] == ["A", "B", "C", "D", "E"]  # walked by cursor token


# -- early stops: a row cap and a recency cutoff ----------------------------------------------

def test_paginate_stops_at_max_rows(httpserver):
    # 2 rows/page, max_rows=3: page 1 (2) + page 2 (2) = 4 >= 3 -> stop; page 3 is never collected.
    httpserver.expect_request("/m1").respond_with_data(_page(["A", "B"], "/m2"), content_type="text/html")
    httpserver.expect_request("/m2").respond_with_data(_page(["C", "D"], "/m3"), content_type="text/html")
    httpserver.expect_request("/m3").respond_with_data(_page(["E", "F"]), content_type="text/html")  # excluded by the cap
    with WebClient() as wc:
        pages = list(wc.fetch(httpserver.url_for("/m1")).paginate(by="link", records="article.r", max_rows=3, max_pages=10))
    assert len(pages) == 2  # the row cap stopped the walk before page 3


def _date_page(dates, next_url=None):
    items = "".join(f'<article class="r"><time class="d">{d}</time></article>' for d in dates)
    nxt = f'<a rel="next" href="{next_url}">Next</a>' if next_url else ""
    return f"<html><body><main>{items}</main>{nxt}</body></html>"


def test_paginate_stops_at_a_recency_cutoff(httpserver):
    # newest-first dates; until/until_before stops once a page reaches records older than the cutoff.
    httpserver.expect_request("/d1").respond_with_data(_date_page(["2026-03-01", "2026-02-01"], "/d2"), content_type="text/html")
    httpserver.expect_request("/d2").respond_with_data(_date_page(["2026-01-15", "2025-12-20"], "/d3"), content_type="text/html")  # oldest < cutoff
    httpserver.expect_request("/d3").respond_with_data(_date_page(["2025-06-01"]), content_type="text/html")  # must NOT be fetched
    with WebClient() as wc:
        pages = list(
            wc.fetch(httpserver.url_for("/d1"))
            .paginate(by="link", until="time.d", until_before="2026-01-01", max_pages=10)
        )
    assert len(pages) == 2  # d1 (all recent), d2 (crosses the cutoff, kept), then stop before d3


# -- by="auto": resolve the advance from the detected pagination hint --------------------------

def test_paginate_auto_detects_a_param_advance(httpserver):
    # by="auto" (the default) reads the page's pagination hint. These pages carry a ?page= LINK
    # (no rel=next), so the hint is kind="param" -> auto walks ?page=1,2. A by="link" fallback
    # could NOT reach page 2 here (there is no rel=next), so 2 pages proves auto picked param.
    def _pg(records, nextp=None):
        arts = "".join(f'<article class="r"><span class="n">{n}</span></article>' for n in records)
        tail = f'<a href="/list?page={nextp}">next</a>' if nextp else '<nav class="pagination">end</nav>'
        return f"<html><body><main>{arts}</main>{tail}</body></html>"

    httpserver.expect_request("/list", query_string="page=1").respond_with_data(_pg(["A", "B"], 2), content_type="text/html")
    httpserver.expect_request("/list", query_string="page=2").respond_with_data(_pg(["C"], 3), content_type="text/html")
    httpserver.expect_request("/list", query_string="page=3").respond_with_data("", status=404)
    with WebClient() as wc:
        pages = list(wc.fetch(httpserver.url_for("/list") + "?page=1").paginate(max_pages=10))  # no by= -> auto
    assert len(pages) == 2  # auto -> by="param" walked page=1,2 (a link fallback would stop at 1)


def test_paginate_auto_falls_back_to_link(httpserver):
    # no page-param, but a rel=next link -> the hint is kind="link" (or absent) -> auto follows it.
    httpserver.expect_request("/a1").respond_with_data(_page(["A"], "/a2"), content_type="text/html")
    httpserver.expect_request("/a2").respond_with_data(_page(["B"]), content_type="text/html")  # no next
    with WebClient() as wc:
        pages = list(wc.fetch(httpserver.url_for("/a1")).paginate(max_pages=10))  # no by= -> auto -> link
    assert len(pages) == 2  # followed rel=next


# -- bound-op power: an Expr stop predicate and an Expr dedup key -------------------------------

def _marked_page(records, next_url=None, mark=False):
    items = "".join(f'<article class="r"><span class="n">{n}</span></article>' for n in records)
    done = '<div class="done"></div>' if mark else ""
    nxt = f'<a rel="next" href="{next_url}">Next</a>' if next_url else ""
    return f"<html><body><main>{items}</main>{done}{nxt}</body></html>"


def test_paginate_stops_on_an_expr_predicate(httpserver):
    # paginate is a BOUND op: the ``stop`` sub-plan is evaluated against each page. Here it stops
    # once a page carries a ``.done`` marker -- BEFORE the natural rel=next end (p3 is never fetched).
    httpserver.expect_request("/s1").respond_with_data(_marked_page(["A"], "/s2"), content_type="text/html")
    httpserver.expect_request("/s2").respond_with_data(_marked_page(["B"], "/s3", mark=True), content_type="text/html")
    httpserver.expect_request("/s3").respond_with_data(_marked_page(["C"], "/s4"), content_type="text/html")  # must NOT be fetched
    plan = (
        wq.reference(httpserver.url_for("/s1")).resolve()
        .paginate(by="link", stop=wq.doc.select("div.done", optional=True).is_ok(), max_pages=10)
        .select_all("article.r").extract(n=wq.doc.select(".n").attr("text")).project()
    )
    assert [r["n"] for r in plan.collect()] == ["A", "B"]  # stopped at the page with .done


def _keyed_page(first, stamp, next_url=None):
    # the first record is the page's identity; a per-request stamp makes the BYTES differ each time
    # (so a content-hash clamp guard would NOT catch a repeat -- only a semantic key does).
    nxt = f'<a rel="next" href="{next_url}">Next</a>' if next_url else ""
    return (
        f'<html><body><main><article class="r"><span class="n">{first}</span></article>'
        f'<time>{stamp}</time></main>{nxt}</body></html>'
    )


def test_paginate_dedups_pages_by_an_expr_key(httpserver):
    # key= gives each page a semantic identity (its first record); a repeat stops the walk even
    # though the raw bytes differ page to page (the <time> stamp changes), which a content hash misses.
    httpserver.expect_request("/k1").respond_with_data(_keyed_page("A", "t1", "/k2"), content_type="text/html")
    httpserver.expect_request("/k2").respond_with_data(_keyed_page("B", "t2", "/k3"), content_type="text/html")
    httpserver.expect_request("/k3").respond_with_data(_keyed_page("A", "t3", "/k4"), content_type="text/html")  # first record repeats
    with WebClient() as wc:
        pages = list(
            wc.fetch(httpserver.url_for("/k1")).paginate(
                by="link", key=wq.doc.select("article.r .n", index=0).attr("text"), max_pages=10
            )
        )
    assert len(pages) == 2  # k1(A), k2(B), then k3's key "A" repeats -> stop (k3 dropped)


# -- next_link(): the HTTP Link header + HTML rel=next -----------------------------------------

def test_next_link_reads_the_http_link_header(httpserver):
    httpserver.expect_request("/api").respond_with_data(
        "[]", content_type="application/json",
        headers={"Link": '<http://api.example/items?page=2>; rel="next", <http://api.example/items?page=9>; rel="last"'},
    )
    with WebClient() as wc:
        nxt = wc.fetch(httpserver.url_for("/api")).next_link()
    assert nxt.ok and nxt.url == "http://api.example/items?page=2"  # the rel=next entry


def test_next_link_reads_html_rel_next_else_empty(httpserver):
    httpserver.expect_request("/a").respond_with_data(_page(["A"], "/b"), content_type="text/html")
    httpserver.expect_request("/end").respond_with_data(_page(["Z"]), content_type="text/html")  # no next
    with WebClient() as wc:
        assert wc.fetch(httpserver.url_for("/a")).next_link().ok           # HTML rel=next present
        assert not wc.fetch(httpserver.url_for("/end")).next_link().ok      # none -> not-ok reference


def test_paginate_follows_the_link_header_for_api_pagination(httpserver):
    # an API-style paginated source: rel=next lives in the HTTP Link header, NOT the HTML.
    httpserver.expect_request("/l1").respond_with_data(
        _page(["A", "B"]), content_type="text/html",
        headers={"Link": f'<{httpserver.url_for("/l2")}>; rel="next"'},
    )
    httpserver.expect_request("/l2").respond_with_data(_page(["C"]), content_type="text/html")  # no next
    plan = (
        wq.reference(httpserver.url_for("/l1")).resolve()
        .paginate(by="link", max_pages=5)
        .select_all("article.r").extract(n=wq.doc.select(".n").attr("text")).project()
    )
    assert [r["n"] for r in plan.collect()] == ["A", "B", "C"]  # walked via the Link header
