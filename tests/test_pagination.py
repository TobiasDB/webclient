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
