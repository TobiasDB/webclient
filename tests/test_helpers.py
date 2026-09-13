"""P5/P6: the final-URL join fix, WebClientCore, and search/summary."""

import pytest

from webclient import WebClient, WebClientCore, doc

RESULTS = """
<html><body>
  <div class="result"><a class="result__a" href="/go/1">First</a></div>
  <div class="result"><a class="result__a" href="/go/2">Second</a></div>
  <div class="result"><a class="result__a" href="/go/3">Third</a></div>
</body></html>
"""


@pytest.fixture
def wc():
    with WebClient() as client:
        yield client


def test_links_join_against_final_url_after_redirect(httpserver, wc):
    from werkzeug.wrappers import Response

    httpserver.expect_request("/old").respond_with_response(
        Response(status=302, headers={"Location": httpserver.url_for("/new/page")})
    )
    httpserver.expect_request("/new/page").respond_with_data(
        "<a href='sibling'>x</a>", content_type="text/html"
    )
    doc = wc.ref(httpserver.url_for("/old")).resolve().collect()
    assert doc.final_url.endswith("/new/page")
    assert doc.select("a").attr("href").path == "/new/sibling"  # not /sibling


def test_search_is_just_an_expression(httpserver, wc):
    """No search verb / engine type: a search is a plan the caller composes
    from the ordinary surface -- resolve, pick results, extract rows."""
    httpserver.expect_request("/s").respond_with_data(RESULTS, content_type="text/html")
    rows = (
        wc.ref(httpserver.url_for("/s"))
        .resolve()
        .select_all(".result")
        .limit(2)
        .extract(
            title=doc.select(".result__a").attr("text"),
            url=doc.select(".result__a").attr("href"),
        )
        .collect()
        .project()
    )
    assert [r["title"] for r in rows] == ["First", "Second"]
    assert rows[0]["url"].path == "/go/1"


def test_summary_returns_title_and_markdown(httpserver, wc):
    httpserver.expect_request("/p").respond_with_data(
        "<html><head><title>Hi</title></head><body><h1>Big</h1></body></html>",
        content_type="text/html",
    )
    out = wc.summary(httpserver.url_for("/p")).collect()
    assert out["ok"] and out["title"] == "Hi" and "Big" in out["markdown"]


def test_core_is_the_async_surface(wc):
    assert isinstance(wc.core, WebClientCore)
