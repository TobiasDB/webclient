"""The Pagination SessionCore (crawl's twin): ``wc.paginate(source, ...)`` -- a manual/agent walk
over a paginated series, driven by a BoundedLoop. run() walks to the end, step() fetches one page,
stream() yields pages as fetched; .verdict carries the precise stop cause. Covers by=param/link/auto,
the manual step surface, streaming, the recency cutoff, and the clamp (out-of-range repeat) guard."""

from webclient import WebClient


def _page(records, next_url=None):
    items = "".join(f'<article class="r"><span class="n">{n}</span></article>' for n in records)
    nxt = f'<a rel="next" href="{next_url}">Next</a>' if next_url else ""
    return f"<html><body><main>{items}</main>{nxt}</body></html>"


def test_paginate_session_run_walks_by_link(httpserver):
    httpserver.expect_request("/p1").respond_with_data(_page(["A", "B"], "/p2"), content_type="text/html")
    httpserver.expect_request("/p2").respond_with_data(_page(["C", "D"], "/p3"), content_type="text/html")
    httpserver.expect_request("/p3").respond_with_data(_page(["E"]), content_type="text/html")  # no next
    with WebClient() as wc:
        with wc.paginate(httpserver.url_for("/p1"), by="link", max_pages=10) as pg:
            pg.run()
            assert pg.done and pg.verdict is not None
            assert pg.verdict.reason == "done" and pg.verdict.stop == "no-next"
            assert pg.verdict.pages == 3
            # the pages are real Documents -- extract across them
            rows = [d.select(".n").attr("text") for page in pg.pages for d in page.select_all("article.r")]
    assert rows == ["A", "B", "C", "D", "E"]


def test_paginate_session_step_is_manual(httpserver):
    httpserver.expect_request("/s1").respond_with_data(_page(["A"], "/s2"), content_type="text/html")
    httpserver.expect_request("/s2").respond_with_data(_page(["B"], "/s3"), content_type="text/html")
    httpserver.expect_request("/s3").respond_with_data(_page(["C"]), content_type="text/html")
    with WebClient() as wc:
        with wc.paginate(httpserver.url_for("/s1"), by="link", max_pages=10) as pg:
            pg.step()  # fetch page one
            assert len(pg.pages) == 1 and not pg.done
            pg.step()  # fetch page two
            assert len(pg.pages) == 2 and not pg.done
            pg.step().step()  # page three, then the terminal round
            assert pg.done and len(pg.pages) == 3


def test_paginate_session_by_param_and_max_pages(httpserver):
    for i in range(1, 8):
        httpserver.expect_request("/list", query_string=f"page={i}").respond_with_data(
            _page([f"r{i}"]), content_type="text/html")
    with WebClient() as wc:
        pages = list(wc.paginate(httpserver.url_for("/list") + "?page=1", by="param", name="page", max_pages=3).stream())
    assert len(pages) == 3  # the page budget capped the walk


def test_paginate_session_auto_detects_the_advance(httpserver):
    # page-param links (no rel=next) -> by="auto" resolves to by="param"
    def pg(records, nextp=None):
        items = "".join(f'<article class="r"><span class="n">{n}</span></article>' for n in records)
        tail = f'<a href="/list?page={nextp}">next</a>' if nextp else '<nav class="pagination">end</nav>'
        return f"<html><body><main>{items}</main>{tail}</body></html>"
    httpserver.expect_request("/list", query_string="page=1").respond_with_data(pg(["A"], 2), content_type="text/html")
    httpserver.expect_request("/list", query_string="page=2").respond_with_data(pg(["B"], 3), content_type="text/html")
    httpserver.expect_request("/list", query_string="page=3").respond_with_data("", status=404)
    with WebClient() as wc:
        walk = wc.paginate(httpserver.url_for("/list") + "?page=1", max_pages=10).run()  # by="auto"
    assert walk.verdict.pages == 2 and walk.verdict.stop == "empty"  # walked page 1,2 then 404


def test_paginate_session_stops_on_a_clamp(httpserver):
    # c2's next loops back to c1 (same content) -> a clamp -> verdict reason "stalled"
    httpserver.expect_request("/c1").respond_with_data(_page(["A"], "/c2"), content_type="text/html")
    httpserver.expect_request("/c2").respond_with_data(_page(["B"], "/c1"), content_type="text/html")
    with WebClient() as wc:
        pg = wc.paginate(httpserver.url_for("/c1"), by="link", max_pages=50).run()
    assert pg.verdict.pages == 2 and pg.verdict.reason == "stalled" and pg.verdict.stop == "clamp"


def test_paginate_session_accepts_a_fetched_document_as_page_one(httpserver):
    # seed with an already-fetched Document -> no re-fetch of page one
    httpserver.expect_request("/d1").respond_with_data(_page(["A"], "/d2"), content_type="text/html")
    httpserver.expect_request("/d2").respond_with_data(_page(["B"]), content_type="text/html")
    with WebClient() as wc:
        doc = wc.fetch(httpserver.url_for("/d1"))
        pg = wc.paginate(doc, by="link", max_pages=10).run()
    assert pg.verdict.pages == 2 and pg.pages[0] is doc  # page one is the passed Document
