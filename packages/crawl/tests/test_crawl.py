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
        c = Crawler(Resolver(HttpFetcher()))
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
        c = Crawler(Resolver(HttpFetcher()))
        try:
            n = 0
            async for _ in c.crawl([httpserver.url_for("/")], max_pages=2):
                n += 1
            return n
        finally:
            await c.aclose()

    assert _run(go()) == 2


def test_paginate_walks_the_next_link(httpserver: HTTPServer) -> None:
    from web.crawl import paginate
    httpserver.expect_request("/p1").respond_with_data(
        b"<p>one</p><a rel='next' href='/p2'>next</a>", content_type="text/html")
    httpserver.expect_request("/p2").respond_with_data(
        b"<p>two</p><a rel='next' href='/p3'>next</a>", content_type="text/html")
    httpserver.expect_request("/p3").respond_with_data(b"<p>three</p>", content_type="text/html")  # no next

    async def go() -> list[str]:
        r = Resolver(HttpFetcher())
        try:
            texts: list[str] = []
            async for doc in paginate(r, httpserver.url_for("/p1"), max_pages=10):
                el = doc.select("p")
                texts.append(el.text if el is not None else "")
            return texts
        finally:
            await r.aclose()

    assert _run(go()) == ["one", "two", "three"]  # walked p1 -> p2 -> p3, stopped (no next)


def test_paginate_respects_max_pages(httpserver: HTTPServer) -> None:
    from web.crawl import paginate
    httpserver.expect_request("/a").respond_with_data(
        b"<a rel='next' href='/a'>loops</a>", content_type="text/html")  # self-loop

    async def go() -> int:
        r = Resolver(HttpFetcher())
        try:
            return len([d async for d in paginate(r, httpserver.url_for("/a"), max_pages=3)])
        finally:
            await r.aclose()

    # the self-link dedups to 1 page (already-seen guard); max_pages also bounds it
    assert _run(go()) == 1


def test_paginate_by_param_increments_and_stops_when_empty(httpserver: HTTPServer) -> None:
    from web.crawl import by_param, paginate
    # ?page=1,2 have items; ?page=3 is empty -> until stops it
    httpserver.expect_request("/items", query_string="page=1").respond_with_data(
        b"<li class='row'>a</li><li class='row'>b</li>", content_type="text/html")
    httpserver.expect_request("/items", query_string="page=2").respond_with_data(
        b"<li class='row'>c</li>", content_type="text/html")
    httpserver.expect_request("/items", query_string="page=3").respond_with_data(
        b"<p>no results</p>", content_type="text/html")

    async def go() -> int:
        r = Resolver(HttpFetcher())
        try:
            rows = 0
            async for doc in paginate(r, httpserver.url_for("/items") + "?page=1",
                                      next_url=by_param("page"),
                                      until=lambda d: not d.select_all(".row"), max_pages=10):
                rows += len(doc.select_all(".row"))
            return rows
        finally:
            await r.aclose()

    assert _run(go()) == 3  # 2 + 1 items; the empty page-3 stopped it (and contributed 0)


def test_paginate_live_clicks_load_more(httpserver: HTTPServer) -> None:
    from web.crawl import paginate_live
    from web.fetch import BrowserFetcher
    # a Load-more button that appends a row and removes itself on the 2nd click
    httpserver.expect_request("/lm").respond_with_data(
        b"<html><body><ul id='list'><li class='row'>1</li></ul>"
        b"<button id='more' onclick=\""
        b"var n=document.querySelectorAll('.row').length+1;"
        b"var li=document.createElement('li');li.className='row';li.textContent=n;"
        b"document.getElementById('list').appendChild(li);"
        b"if(n>=3)this.remove();\">more</button></body></html>",
        content_type="text/html")

    async def go() -> list[int]:
        bf = BrowserFetcher()
        try:
            counts: list[int] = []
            async for doc in paginate_live(bf, httpserver.url_for("/lm"), more="#more", max_pages=10):
                counts.append(len(doc.select_all(".row")))
            return counts
        finally:
            await bf.aclose()

    counts = _run(go())
    # content accumulates: 1 row, then 2, then 3 (button gone) -> stops
    assert counts[-1] == 3 and counts == sorted(counts)
