"""SearchBacking: the client's ``search`` verb -> structured SearchResult hits.

The results page is served locally (a canned DuckDuckGo-shaped document), so the
test exercises the real fetch + parse path with no network.
"""

import asyncio

import pytest

from webclient import AsyncWebClient, SearchResult, WebClient

# a DDG-shaped results page: three real hits (the third via a DDG redirect link),
# one non-result row (an ad container with no ``.result__a``) that must be skipped.
RESULTS_HTML = """
<html><body>
  <div class="result result--ad"><span>Sponsored</span></div>
  <div class="result">
    <a class="result__a" href="https://example.com/aeropress">Aeropress Guide</a>
    <a class="result__snippet">How to brew a great cup with an Aeropress.</a>
  </div>
  <div class="result">
    <a class="result__a" href="https://example.com/grinder">Best Grinders</a>
    <a class="result__snippet">A roundup of burr grinders for espresso.</a>
  </div>
  <div class="result">
    <a class="result__a"
       href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fkettle&amp;rut=abc">
       Gooseneck Kettles</a>
    <a class="result__snippet">Pouring control for pour-over coffee.</a>
  </div>
</body></html>
"""


@pytest.fixture
def wc():
    with WebClient() as client:
        yield client


@pytest.fixture
def search_endpoint(httpserver):
    httpserver.expect_request("/html/").respond_with_data(
        RESULTS_HTML, content_type="text/html; charset=utf-8"
    )
    return httpserver.url_for("/html/")


def test_search_returns_structured_hits(wc, search_endpoint):
    hits = wc.search("coffee", endpoint=search_endpoint)
    assert all(isinstance(h, SearchResult) for h in hits)
    # the ad row is skipped; the three real results come back in order.
    assert [h.title for h in hits] == [
        "Aeropress Guide",
        "Best Grinders",
        "Gooseneck Kettles",
    ]
    assert [h.rank for h in hits] == [1, 2, 3]
    first = hits[0]
    assert first.url == "https://example.com/aeropress"
    assert first.description == "How to brew a great cup with an Aeropress."


def test_search_unwraps_duckduckgo_redirect(wc, search_endpoint):
    hits = wc.search("coffee", endpoint=search_endpoint)
    # the redirect link (``/l/?uddg=...``) is unwrapped to its real destination.
    assert hits[2].url == "https://example.com/kettle"


def test_search_respects_limit(wc, search_endpoint):
    hits = wc.search("coffee", endpoint=search_endpoint, limit=1)
    assert [h.title for h in hits] == ["Aeropress Guide"]


def test_search_is_json_serialisable(wc, search_endpoint):
    hit = wc.search("coffee", endpoint=search_endpoint)[0]
    assert hit.model_dump() == {
        "rank": 1,
        "title": "Aeropress Guide",
        "url": "https://example.com/aeropress",
        "description": "How to brew a great cup with an Aeropress.",
    }


def test_search_lazy_records_and_collects(wc, search_endpoint):
    hits = wc.lazy.search("coffee", endpoint=search_endpoint).collect()
    assert [h.title for h in hits] == [
        "Aeropress Guide",
        "Best Grinders",
        "Gooseneck Kettles",
    ]


def test_search_async(search_endpoint):
    async def main():
        async with AsyncWebClient() as ac:
            return await ac.search("coffee", endpoint=search_endpoint)

    hits = asyncio.run(main())
    assert [h.rank for h in hits] == [1, 2, 3]
    assert hits[1].url == "https://example.com/grinder"
