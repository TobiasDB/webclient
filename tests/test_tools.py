"""The task-verb layer (webclient.tools): thin markdown/rows helpers."""

import pytest

from webclient import WebClient
from webclient.tools import extract, fetch_markdown, fetch_text, links

PAGE = """
<html><head><title>Shop</title></head><body>
  <h1>Featured</h1><p>Curated picks.</p>
  <div class="card"><span class="title">Aeropress</span><a href="/i/1">go</a></div>
  <div class="card"><span class="title">Grinder</span><a href="/i/2">go</a></div>
</body></html>
"""


@pytest.fixture
def wc():
    with WebClient() as client:
        yield client


def test_fetch_markdown(httpserver, wc):
    httpserver.expect_request("/p").respond_with_data(PAGE, content_type="text/html")
    md = fetch_markdown(httpserver.url_for("/p"), client=wc)
    assert "# Featured" in md and "Curated picks." in md


def test_fetch_text(httpserver, wc):
    httpserver.expect_request("/p").respond_with_data(PAGE, content_type="text/html")
    text = fetch_text(httpserver.url_for("/p"), client=wc)
    assert "Curated picks." in text


def test_links(httpserver, wc):
    httpserver.expect_request("/p").respond_with_data(PAGE, content_type="text/html")
    urls = links(httpserver.url_for("/p"), client=wc)
    assert all(u.startswith("http") for u in urls)
    assert any(u.endswith("/i/1") for u in urls)


def test_extract(httpserver, wc):
    httpserver.expect_request("/p").respond_with_data(PAGE, content_type="text/html")
    rows = extract(httpserver.url_for("/p"), ".card", {"title": ".title"}, client=wc)
    assert [r["title"] for r in rows] == ["Aeropress", "Grinder"]


def test_extract_with_limit(httpserver, wc):
    httpserver.expect_request("/p").respond_with_data(PAGE, content_type="text/html")
    rows = extract(
        httpserver.url_for("/p"), ".card", {"title": ".title"}, limit=1, client=wc
    )
    assert [r["title"] for r in rows] == ["Aeropress"]
