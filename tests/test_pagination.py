"""Pagination: ``doc.paginate(...)`` walks a dataset's pages into a Collection[Document], and the rest of
the chain extracts ACROSS every page. A pager is ONE iterator (next= / pages= / cursor= / click= /
scroll=) plus until= / filter= / max_pages= / records= (docs/product/pagination.md). Covers each
iterator, every stop cause, the removed by= API, and the regressions the redesign fixed (an offset
walked by 1, a walk started on page 3 going back, later pages fetched off page one's tier)."""

import pytest

from webclient import WebClient
from webclient.errors import WebException
from webclient.interface import wq


def _page(records, next_url=None):
    items = "".join(f'<article class="r"><span class="n">{n}</span></article>' for n in records)
    nxt = f'<a rel="next" href="{next_url}">Next</a>' if next_url else ""
    return f"<html><body><main>{items}</main>{nxt}</body></html>"


def _rows(plan):
    return [r["n"] for r in plan.select_all("article.r").extract(n=wq.doc.select(".n").attr("text")).project().collect()]


def _html(httpserver, path, body, qs=None, **kw):
    req = httpserver.expect_request(path, query_string=qs) if qs is not None else httpserver.expect_request(path)
    req.respond_with_data(body, content_type="text/html", **kw)


def test_paginate_next_link_returns_all_pages(httpserver):
    _html(httpserver, "/p1", _page(["A", "B"], "/p2"))
    _html(httpserver, "/p2", _page(["C", "D"], "/p3"))
    _html(httpserver, "/p3", _page(["E"]))  # no next
    with WebClient() as wc:
        pages = wc.fetch(httpserver.url_for("/p1")).paginate(next=wq.doc.next_link(), max_pages=10)
    assert len(list(pages)) == 3  # followed rel=next to the end


def test_paginate_extracts_the_whole_dataset(httpserver):
    _html(httpserver, "/p1", _page(["A", "B"], "/p2"))
    _html(httpserver, "/p2", _page(["C", "D"], "/p3"))
    _html(httpserver, "/p3", _page(["E"]))
    plan = wq.reference(httpserver.url_for("/p1")).resolve().paginate(next=wq.doc.next_link())
    assert _rows(plan) == ["A", "B", "C", "D", "E"]  # flat across all 3 pages


def test_paginate_next_selector_shorthand(httpserver):
    # a site whose "next" control has no rel=next: a str next= is a selector whose href is followed
    def page(records, nxt=None):
        items = "".join(f'<article class="r"><span class="n">{n}</span></article>' for n in records)
        link = f'<ul class="pager"><li class="next"><a href="{nxt}">next »</a></li></ul>' if nxt else '<ul class="pager"></ul>'
        return f"<html><body><main>{items}</main>{link}</body></html>"
    _html(httpserver, "/a", page(["A"], "/b"))
    _html(httpserver, "/b", page(["B"], "/c"))
    _html(httpserver, "/c", page(["C"]))
    assert _rows(wq.reference(httpserver.url_for("/a")).resolve().paginate(next="li.next a")) == ["A", "B", "C"]
    # ... the same as an Expr reading the href
    plan = wq.reference(httpserver.url_for("/a")).resolve().paginate(next=wq.doc.select("li.next a", optional=True).attr("href", optional=True))
    assert _rows(plan) == ["A", "B", "C"]


def test_paginate_pages_walks_a_param_until_empty(httpserver):
    for i, recs in enumerate([["A", "B"], ["C", "D"], ["E"]], 1):
        _html(httpserver, "/list", _page(recs), qs=f"page={i}")
    httpserver.expect_request("/list", query_string="page=4").respond_with_data("", status=404)
    with WebClient() as wc:
        pg = wc.paginate(httpserver.url_for("/list") + "?page=1", pages="page").run()
    assert len(pg.pages) == 3 and pg.verdict.stop == "empty"
    assert _rows(wq.reference(httpserver.url_for("/list") + "?page=1").resolve().paginate(pages="page")) == ["A", "B", "C", "D", "E"]


def test_paginate_pages_offset_steps_by_the_page_size(httpserver):
    # regression: an offset param walked by 1 (0,1,2,…: overlapping pages). step= is the page size.
    data = [f"r{i}" for i in range(7)]
    for off in range(0, 8, 3):
        _html(httpserver, "/o", _page(data[off:off + 3]), qs=f"offset={off}")
    httpserver.expect_request("/o", query_string="offset=9").respond_with_data("", status=404)
    plan = wq.reference(httpserver.url_for("/o") + "?offset=0").resolve().paginate(pages="offset", step=3, records="article.r")
    assert _rows(plan) == data  # 0,3,6 -- each record once


def test_paginate_pages_starts_from_the_current_page(httpserver):
    # regression: a walk started on ?page=3 went BACK to 2 -- it starts at the URL's value and goes forward
    for i in range(1, 6):
        _html(httpserver, "/l", _page([f"p{i}"]), qs=f"page={i}")
    plan = wq.reference(httpserver.url_for("/l") + "?page=3").resolve().paginate(pages="page", stop=5)
    assert _rows(plan) == ["p3", "p4", "p5"]
    # an explicit start= different from the URL's fetches start as page one
    plan = wq.reference(httpserver.url_for("/l") + "?page=3").resolve().paginate(pages="page", start=1, stop=2)
    assert _rows(plan) == ["p1", "p2"]


def test_paginate_pages_with_a_known_stop_fetches_concurrently(httpserver):
    # pages= + a known stop (and nothing to test per page): every page is known up front -> one
    # concurrent batch. In order; an over-estimated stop still ends at the real end.
    for i in range(1, 6):
        _html(httpserver, "/list", _page([f"r{i}"]), qs=f"page={i}")
    httpserver.expect_request("/list", query_string="page=6").respond_with_data("", status=404)
    with WebClient() as wc:
        pg = wc.paginate(httpserver.url_for("/list") + "?page=1", pages="page", stop=8).run()
    assert [p.url.rsplit("=", 1)[1] for p in pg.pages] == ["1", "2", "3", "4", "5"] and pg.verdict.stop == "empty"
    assert pg.verdict.rounds == 2  # page one, then ONE batch


def test_paginate_pages_stop_is_read_off_page_one(httpserver):
    def page(n):
        return f'<html><body><main><article class="r"><span class="n">p{n}</span></article></main><a class="last" href="?page=3">3</a></body></html>'
    for i in range(1, 5):
        _html(httpserver, "/s", page(i), qs=f"page={i}")  # a 4th page exists, but the pager's last is 3
    plan = wq.reference(httpserver.url_for("/s") + "?page=1").resolve().paginate(pages="page", stop=wq.doc.select("a.last").attr("text").number())
    assert _rows(plan) == ["p1", "p2", "p3"]


def test_paginate_is_bounded_by_max_pages(httpserver):
    for i in range(1, 30):
        _html(httpserver, f"/n{i}", _page([f"r{i}"], f"/n{i+1}"))
    with WebClient() as wc:
        pg = wc.paginate(httpserver.url_for("/n1"), next=wq.doc.next_link(), max_pages=5).run()
    assert len(pg.pages) == 5 and pg.verdict.stop == "budget" and pg.verdict.reason == "budget"


def test_paginate_stops_on_a_repeat(httpserver):
    # c2's "next" points back to c1 -> a repeat stops the walk rather than cycling
    _html(httpserver, "/c1", _page(["A"], "/c2"))
    _html(httpserver, "/c2", _page(["B"], "/c1"))
    with WebClient() as wc:
        pg = wc.paginate(httpserver.url_for("/c1"), next=wq.doc.next_link(), max_pages=50).run()
    assert len(pg.pages) == 2 and pg.verdict.stop == "repeat" and pg.verdict.reason == "stalled"


def test_paginate_repeat_is_compared_by_records(httpserver):
    # an out-of-range CLAMP that re-serves page 2 with a fresh timestamp: the bytes differ, the
    # records do not -> with records= the repeat is caught
    def page(recs, stamp):
        return _page(recs).replace("</main>", f"</main><time>{stamp}</time>")
    _html(httpserver, "/k", page(["A"], "t1"), qs="page=1")
    _html(httpserver, "/k", page(["B"], "t2"), qs="page=2")
    _html(httpserver, "/k", page(["B"], "t3"), qs="page=3")  # the clamp
    with WebClient() as wc:
        pg = wc.paginate(httpserver.url_for("/k") + "?page=1", pages="page", records="article.r").run()
    assert len(pg.pages) == 2 and pg.verdict.stop == "repeat"


def test_paginate_repeat_ignores_scripts(httpserver):
    # without records=, a page is compared by its content WITHOUT scripts (a nonce is not new content)
    def page(recs, nonce):
        return _page(recs).replace("</main>", f"</main><script>var nonce='{nonce}'</script>")
    _html(httpserver, "/k", page(["A"], "n1"), qs="page=1")
    _html(httpserver, "/k", page(["B"], "n2"), qs="page=2")
    _html(httpserver, "/k", page(["B"], "n3"), qs="page=3")
    with WebClient() as wc:
        pg = wc.paginate(httpserver.url_for("/k") + "?page=1", pages="page").run()
    assert len(pg.pages) == 2 and pg.verdict.stop == "repeat"


def _cursor_page(records, cursor=None):
    items = "".join(f'<article class="r"><span class="n">{n}</span></article>' for n in records)
    more = f'<a class="more" data-cursor="{cursor}">More</a>' if cursor else ""
    return f"<html><body><main>{items}</main>{more}</body></html>"


def test_paginate_cursor_follows_a_token(httpserver):
    _html(httpserver, "/feed", _cursor_page(["A", "B"], "k2"), qs="")
    _html(httpserver, "/feed", _cursor_page(["C", "D"], "k3"), qs="after=k2")
    _html(httpserver, "/feed", _cursor_page(["E"]), qs="after=k3")  # no token -> the end
    plan = wq.reference(httpserver.url_for("/feed")).resolve().paginate(
        cursor=wq.doc.select("a.more", optional=True).attr("data-cursor", optional=True), param="after")
    assert _rows(plan) == ["A", "B", "C", "D", "E"]


def _date_page(dates, next_url=None):
    items = "".join(f'<article class="r"><span class="n">{d}</span><time class="d">{d}</time></article>' for d in dates)
    nxt = f'<a rel="next" href="{next_url}">Next</a>' if next_url else ""
    return f"<html><body><main>{items}</main>{nxt}</body></html>"


def test_paginate_until_stops_at_the_newest_data(httpserver):
    # newest-first: until= the page's OLDEST date is before the cutoff -> that page is the last (kept)
    _html(httpserver, "/d1", _date_page(["2026-03-01", "2026-02-01"], "/d2"))
    _html(httpserver, "/d2", _date_page(["2026-01-15", "2025-12-20"], "/d3"))  # crosses the cutoff
    _html(httpserver, "/d3", _date_page(["2025-06-01"]))  # must NOT be fetched
    with WebClient() as wc:
        pg = wc.paginate(httpserver.url_for("/d1"), next=wq.doc.next_link(),
                         until=wq.doc.select("article.r:last-child time.d").attr("text") < "2026-01-01").run()
    assert len(pg.pages) == 2 and pg.verdict.stop == "until"


def test_paginate_until_a_marker(httpserver):
    def page(recs, nxt, mark=False):
        return _page(recs, nxt).replace("</main>", '</main><div class="done"></div>' if mark else "</main>")
    _html(httpserver, "/s1", page(["A"], "/s2"))
    _html(httpserver, "/s2", page(["B"], "/s3", mark=True))
    _html(httpserver, "/s3", page(["C"], "/s4"))  # must NOT be fetched
    plan = wq.reference(httpserver.url_for("/s1")).resolve().paginate(
        next=wq.doc.next_link(), until=wq.doc.select("div.done", optional=True).is_ok())
    assert _rows(plan) == ["A", "B"]


def test_paginate_filter_keeps_pages_and_walks_on(httpserver):
    # filter= keeps a page only when it holds; the walk goes on past the dropped ones
    def page(recs, nxt, tag):
        return _page(recs, nxt).replace("<main>", f'<main data-kind="{tag}">')
    _html(httpserver, "/f1", page(["A"], "/f2", "news"))
    _html(httpserver, "/f2", page(["B"], "/f3", "ad"))
    _html(httpserver, "/f3", page(["C"], None, "news"))
    with WebClient() as wc:
        pg = wc.paginate(httpserver.url_for("/f1"), next=wq.doc.next_link(),
                         filter=wq.doc.select("main").attr("data-kind") == "news").run()
    assert len(pg.pages) == 2 and pg.verdict.fetched == 3 and pg.verdict.stop == "end"


def test_paginate_later_pages_use_page_ones_tier(httpserver, monkeypatch):
    # regression: page one needed the browser, but its next pages were fetched static
    _html(httpserver, "/t1", _page(["A"], "/t2"))
    _html(httpserver, "/t2", _page(["B"]))
    with WebClient() as wc:
        doc = wc.fetch(httpserver.url_for("/t1"))
        doc._tiers = ["static", "browser"]  # as if it escalated
        engine = doc._client
        seen, real = [], type(engine).afetch

        async def spy(self, ref, **kw):
            seen.append(kw.get("browser"))
            return await real(self, ref, **{**kw, "browser": False})
        monkeypatch.setattr(type(engine), "afetch", spy)
        pages = doc.paginate(next=wq.doc.next_link())
    assert len(list(pages)) == 2 and seen == [True]


def test_paginate_link_header_for_api_pagination(httpserver):
    httpserver.expect_request("/l1").respond_with_data(
        _page(["A", "B"]), content_type="text/html",
        headers={"Link": f'<{httpserver.url_for("/l2")}>; rel="next"'},
    )
    _html(httpserver, "/l2", _page(["C"]))
    assert _rows(wq.reference(httpserver.url_for("/l1")).resolve().paginate(next=wq.doc.next_link())) == ["A", "B", "C"]


def test_next_link_reads_the_http_link_header(httpserver):
    httpserver.expect_request("/api").respond_with_data(
        "[]", content_type="application/json",
        headers={"Link": '<http://api.example/items?page=2>; rel="next", <http://api.example/items?page=9>; rel="last"'},
    )
    with WebClient() as wc:
        nxt = wc.fetch(httpserver.url_for("/api")).next_link()
    assert nxt.ok and nxt.url == "http://api.example/items?page=2"  # the rel=next entry


def test_next_link_reads_html_rel_next_else_empty(httpserver):
    _html(httpserver, "/a", _page(["A"], "/b"))
    _html(httpserver, "/end", _page(["Z"]))
    with WebClient() as wc:
        assert wc.fetch(httpserver.url_for("/a")).next_link().ok
        assert not wc.fetch(httpserver.url_for("/end")).next_link().ok


@pytest.mark.parametrize("kwargs, needle", [
    ({}, "no iterator"),
    ({"pages": "page", "next": "a.next"}, "ONE iterator"),
    ({"cursor": "a.more"}, "param="),
    ({"by": "link"}, "by= ->"),
    ({"pages": "page", "max_rows": 5}, "until="),
])
def test_paginate_refuses_a_malformed_pager(httpserver, kwargs, needle):
    _html(httpserver, "/x", _page(["A"]))
    with WebClient() as wc:
        doc = wc.fetch(httpserver.url_for("/x"))
        with pytest.raises(WebException) as e:
            doc.paginate(**kwargs)
    assert e.value.error.code == "paginate.invalid" and needle in e.value.error.message


LOAD_MORE = """<html><body><main id="list"><article class="r"><span class="n">A</span></article></main>
<button id="more">load more</button>
<script>
let n = 0;
document.getElementById('more').addEventListener('click', () => {
  n += 1;
  setTimeout(() => {
    const el = document.createElement('article'); el.className = 'r';
    el.innerHTML = '<span class="n">' + ['B', 'C', 'D'][n - 1] + '</span>';
    document.getElementById('list').appendChild(el);
    if (n >= 3) document.getElementById('more').remove();
  }, 60);
});
</script></body></html>"""


@pytest.mark.parametrize("click", ["#more", "expr"])
def test_paginate_click_loads_more_in_a_plan(httpserver, click):
    pytest.importorskip("playwright")
    _html(httpserver, "/more", LOAD_MORE)
    pager = wq.doc.click("#more", timeout=1) if click == "expr" else click  # an Expr click waits its timeout for a gone control
    plan = (
        wq.reference(httpserver.url_for("/more")).resolve(browser=True)
        .paginate(click=pager, records="article.r", max_pages=10)
        .select_all("article.r").extract(n=wq.doc.select(".n").attr("text")).project()
    )
    with WebClient() as wc:
        rows = wc.execute(plan)
    assert [r["n"] for r in rows] == ["A", "B", "C", "D"]


def test_dataset_reads_the_listing_and_its_recipes(httpserver):
    body = (b'<html><body><select name="sort"><option>New</option></select><main>'
            b'<article class="r"><time datetime="2026-03-01">a</time></article>'
            b'<article class="r"><time datetime="2026-02-01">b</time></article>'
            b'<article class="r"><time datetime="2026-01-01">c</time></article></main>'
            b'<p>Showing 1-3 of 30</p><a href="/list?page=2&amp;cat=x">2</a></body></html>')
    httpserver.expect_request("/list").respond_with_data(body, content_type="text/html")
    with WebClient() as wc:
        ds = wc.fetch(httpserver.url_for("/list") + "?page=1&cat=x").dataset()
    assert ds.paginated is not None and ds.paginated.best.mode == "pages"
    assert ds.recipes["all"].endswith('.paginate(pages="page", start=1, step=1, stop=10)')
    assert "paginated" in ds.summary and ds.recipes["latest"]
