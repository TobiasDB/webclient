"""The Pagination SessionCore (crawl's twin): ``wc.paginate(source, ...)`` -- a manual/agent walk
over a paginated series: the SAME pager as doc.paginate (one config, one BoundedLoop), stepped. run()
walks to the end, step() is one round, stream() yields pages as they land; .verdict carries the stop
cause. Covers next= / pages=, the manual step surface, streaming, the repeat guard, a fetched page one
and a paginated remote plan's kwargs."""

from webclient import WebClient
from webclient.interface import wq


def _page(records, next_url=None):
    items = "".join(f'<article class="r"><span class="n">{n}</span></article>' for n in records)
    nxt = f'<a rel="next" href="{next_url}">Next</a>' if next_url else ""
    return f"<html><body><main>{items}</main>{nxt}</body></html>"


def test_paginate_session_run_walks_by_link(httpserver):
    httpserver.expect_request("/p1").respond_with_data(_page(["A", "B"], "/p2"), content_type="text/html")
    httpserver.expect_request("/p2").respond_with_data(_page(["C", "D"], "/p3"), content_type="text/html")
    httpserver.expect_request("/p3").respond_with_data(_page(["E"]), content_type="text/html")  # no next
    with WebClient() as wc:
        with wc.paginate(httpserver.url_for("/p1"), next=wq.doc.next_link(), max_pages=10) as pg:
            pg.run()
            assert pg.done and pg.verdict is not None
            assert pg.verdict.reason == "done" and pg.verdict.stop == "end"
            assert pg.verdict.pages == 3
            # the pages are real Documents -- extract across them
            rows = [d.select(".n").attr("text") for page in pg.pages for d in page.select_all("article.r")]
    assert rows == ["A", "B", "C", "D", "E"]


def test_paginate_session_step_is_manual(httpserver):
    httpserver.expect_request("/s1").respond_with_data(_page(["A"], "/s2"), content_type="text/html")
    httpserver.expect_request("/s2").respond_with_data(_page(["B"], "/s3"), content_type="text/html")
    httpserver.expect_request("/s3").respond_with_data(_page(["C"]), content_type="text/html")
    with WebClient() as wc:
        with wc.paginate(httpserver.url_for("/s1"), next=wq.doc.next_link(), max_pages=10) as pg:
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
        pages = list(wc.paginate(httpserver.url_for("/list") + "?page=1", pages="page", max_pages=3).stream())
    assert len(pages) == 3  # the page budget capped the walk


def test_paginate_session_writes_the_hinted_pager(httpserver):
    # detection only HINTS: the hint's best mode is the pager to write -- page-param links -> pages=
    def pg(records, nextp=None):
        items = "".join(f'<article class="r"><span class="n">{n}</span></article>' for n in records)
        tail = f'<a href="/list?page={nextp}">next</a>' if nextp else '<nav class="pagination">end</nav>'
        return f"<html><body><main>{items}</main>{tail}</body></html>"
    httpserver.expect_request("/list", query_string="page=1").respond_with_data(pg(["A"], 2), content_type="text/html")
    httpserver.expect_request("/list", query_string="page=2").respond_with_data(pg(["B"], 3), content_type="text/html")
    httpserver.expect_request("/list", query_string="page=3").respond_with_data("", status=404)
    with WebClient() as wc:
        doc = wc.fetch(httpserver.url_for("/list") + "?page=1")
        best = doc.pagination().value.best
        assert best.mode == "pages" and best.code == '.paginate(pages="page", start=1, step=1)'
        walk = wc.paginate(doc, pages=best.param, start=best.start, step=best.step, max_pages=10).run()
    assert walk.verdict.pages == 2 and walk.verdict.stop == "empty"  # walked page 1,2 then 404


def test_paginate_session_stops_on_a_clamp(httpserver):
    # c2's next loops back to c1 (same content) -> a clamp -> verdict reason "stalled"
    httpserver.expect_request("/c1").respond_with_data(_page(["A"], "/c2"), content_type="text/html")
    httpserver.expect_request("/c2").respond_with_data(_page(["B"], "/c1"), content_type="text/html")
    with WebClient() as wc:
        pg = wc.paginate(httpserver.url_for("/c1"), next=wq.doc.next_link(), max_pages=50).run()
    assert pg.verdict.pages == 2 and pg.verdict.reason == "stalled" and pg.verdict.stop == "repeat"


def test_paginate_session_accepts_a_fetched_document_as_page_one(httpserver):
    # seed with an already-fetched Document -> no re-fetch of page one
    httpserver.expect_request("/d1").respond_with_data(_page(["A"], "/d2"), content_type="text/html")
    httpserver.expect_request("/d2").respond_with_data(_page(["B"]), content_type="text/html")
    with WebClient() as wc:
        doc = wc.fetch(httpserver.url_for("/d1"))
        pg = wc.paginate(doc, next="a[rel=next]", max_pages=10).run()
    assert pg.verdict.pages == 2 and pg.pages[0] is doc  # page one is the passed Document


def test_paginate_session_remote_plan_carries_the_pager(httpserver):
    # remotely the session runs as ONE plan: reference(url).resolve().paginate(<the set kwargs>)
    with WebClient() as wc:
        pg = wc.paginate("https://x.test/list?page=2", pages="page", step=2, max_pages=5)
        plan = pg._remote_expr()
    text = str(plan)
    assert "paginate(" in text and "pages=" in text and "step=2" in text and "max_pages=5" in text
    assert "by=" not in text
