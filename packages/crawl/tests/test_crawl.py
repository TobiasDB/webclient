"""web.crawl tests -- the frontier over resolve, against a local httpserver."""

from __future__ import annotations

import asyncio

from pytest_httpserver import HTTPServer

from web.fetch import HttpFetcher
from web.parse import Document
from web.resolve import Resolver
from web.crawl import Crawler, Goal


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
            async for doc in c.crawl(Goal(start=httpserver.url_for("/"), max_pages=10)):
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
            async for _ in c.crawl(Goal(start=httpserver.url_for("/"), max_pages=2)):
                n += 1
            return n
        finally:
            await c.aclose()

    assert _run(go()) == 2



def test_authenticated_crawl_carries_session_cookies(httpserver: HTTPServer) -> None:
    from werkzeug.wrappers import Response

    from web.fetch import Request

    httpserver.expect_request("/login").respond_with_response(
        Response(b"ok", headers={"Set-Cookie": "auth=1; Path=/"}))

    def protected(body: bytes) -> "object":
        def handler(req: object) -> Response:
            if req.cookies.get("auth") == "1":  # type: ignore[attr-defined]
                return Response(body, content_type="text/html")
            return Response(b"<p>denied</p>", content_type="text/html")
        return handler

    httpserver.expect_request("/p1").respond_with_handler(protected(b"<p class='ok'>one</p><a href='/p2'>2</a>"))
    httpserver.expect_request("/p2").respond_with_handler(protected(b"<p class='ok'>two</p>"))

    async def with_session() -> list[str]:
        base = Resolver()
        s = await base.session()  # a persistent session over the ladder (a cookie jar)
        try:
            await s.resolve(Request(url=httpserver.url_for("/login")))  # log in -> cookie in the jar
            out: list[str] = []
            async for doc in Crawler(s).crawl(Goal(start=httpserver.url_for("/p1"))):
                el = doc.select(".ok")  # the session cookie carries into every crawled page
                out.append(el.text if el is not None else "denied")
            return out
        finally:
            await s.aclose()
            await base.aclose()

    async def without_session() -> list[str]:
        r = Resolver()
        try:
            out: list[str] = []
            async for doc in Crawler(r).crawl(Goal(start=httpserver.url_for("/p1"))):
                el = doc.select(".ok")
                out.append(el.text if el is not None else "denied")
            return out
        finally:
            await r.aclose()

    assert sorted(_run(with_session())) == ["one", "two"]  # cookie carried -> both protected pages
    assert _run(without_session()) == ["denied"]           # no session -> denied, no links to follow
