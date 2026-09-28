"""web.crawl tests -- the frontier over resolve, against a local httpserver."""

from __future__ import annotations

import asyncio

from pytest_httpserver import HTTPServer

from web.fetch import HttpFetcher
from web.parse import Document
from web.resolve import Resolver
from web.crawl import Crawler


def _run(coro):
    return asyncio.run(coro)


def test_crawl_follows_same_origin_links_bfs(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/").respond_with_data(
        b"<a href='/a'>a</a><a href='/b'>b</a><a href='https://other.example/x'>ext</a>",
        content_type="text/html")
    httpserver.expect_request("/a").respond_with_data(b"<a href='/c'>c</a>", content_type="text/html")
    httpserver.expect_request("/b").respond_with_data(b"<p>b</p>", content_type="text/html")
    httpserver.expect_request("/c").respond_with_data(b"<p>c</p>", content_type="text/html")

    async def go() -> list[str]:
        c = Crawler(Resolver())
        try:
            urls: list[str] = []
            async for doc in c.crawl([httpserver.url_for("/")], max_pages=10):
                assert isinstance(doc, Document)
                urls.append(doc.url)
            return urls
        finally:
            await c.aclose()

    seen = _run(go())
    assert any(u.endswith("/a") for u in seen) and any(u.endswith("/c") for u in seen)
    assert not any("other.example" in u for u in seen)  # off-origin not followed


def test_max_pages_bounds_the_crawl(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/").respond_with_data(
        b"<a href='/a'>a</a><a href='/b'>b</a>", content_type="text/html")
    httpserver.expect_request("/a").respond_with_data(b"<p>a</p>", content_type="text/html")
    httpserver.expect_request("/b").respond_with_data(b"<p>b</p>", content_type="text/html")

    async def go() -> int:
        c = Crawler(Resolver())
        try:
            n = 0
            async for _ in c.crawl([httpserver.url_for("/")], max_pages=2):
                n += 1
            return n
        finally:
            await c.aclose()

    assert _run(go()) == 2

