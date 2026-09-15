"""P5/P6: the final-URL join fix, WebClient, and search-as-an-expression."""

import pytest

from webclient import WebClient, WebClient, doc

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
            title=doc.select(".result__a").text_content,
            url=doc.select(".result__a").attr("href"),
        )
        .collect()
        .project()
    )
    assert [r["title"] for r in rows] == ["First", "Second"]
    assert rows[0]["url"].endswith("/go/1")  # project renders a Reference as its URL


def test_facets_compose_a_page_overview(httpserver, wc):
    # summary is no longer a base backing: a page overview is just the facet ops
    # composed by the caller from the ordinary surface.
    httpserver.expect_request("/p").respond_with_data(
        "<html><head><title>Hi</title></head><body><h1>Big</h1></body></html>",
        content_type="text/html",
    )
    doc = wc.fetch(httpserver.url_for("/p"))
    assert doc.transport().ok
    assert doc.metadata().title == "Hi"
    assert doc.structure().toc[0].text == "Big"


def test_core_is_the_async_surface(wc):
    assert isinstance(wc.core, WebClient)


def test_project_into_a_pydantic_model(httpserver, wc):
    """project(model) validates each extracted row into a typed model."""
    from pydantic import BaseModel

    class Hit(BaseModel):
        title: str

    httpserver.expect_request("/s").respond_with_data(RESULTS, content_type="text/html")
    hits = (
        wc.ref(httpserver.url_for("/s"))
        .resolve()
        .select_all(".result")
        .extract(title=doc.select(".result__a").text_content)
        .collect()
        .project(Hit)
    )
    assert all(isinstance(h, Hit) for h in hits)
    assert [h.title for h in hits] == ["First", "Second", "Third"]
