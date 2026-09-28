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


# -- canonical dedup, sitemap seeding, robots --
from web.crawl import canonical, parse_robots, sitemap_urls  # noqa: E402


def test_canonical_folds_tracking_and_trailing_slash() -> None:
    a = canonical("HTTPS://Ex.com:443/p/?utm_source=x&b=2&a=1#frag")
    b = canonical("https://ex.com/p?a=1&b=2")
    assert a == b == "https://ex.com/p?a=1&b=2"


def test_crawl_dedups_by_canonical_url(httpserver: HTTPServer) -> None:
    # the index links to the same page two ways (bare + tracking param); it must be fetched once
    httpserver.expect_request("/").respond_with_data(
        b"<a href='/p'>x</a><a href='/p?utm_source=news'>x again</a>", content_type="text/html")
    httpserver.expect_request("/p").respond_with_data(b"<p>page</p>", content_type="text/html")

    async def go() -> int:
        c = Crawler(Resolver())
        try:
            return len([d async for d in c.crawl(Goal(start=httpserver.url_for("/"), max_pages=10))])
        finally:
            await c.aclose()

    assert _run(go()) == 2  # index + /p once (not the ?utm variant)


def test_result_rel_canonical_dedups_yields(httpserver: HTTPServer) -> None:
    # two distinct URLs that both declare the SAME canonical -> yielded once
    canon = httpserver.url_for("/article")
    page = f"<link rel=canonical href='{canon}'><p>hi</p>".encode()
    httpserver.expect_request("/").respond_with_data(
        b"<a href='/article'>a</a><a href='/article?page=2'>a2</a>", content_type="text/html")
    httpserver.expect_request("/article").respond_with_data(page, content_type="text/html")
    httpserver.expect_request("/article", query_string="page=2").respond_with_data(page, content_type="text/html")

    async def go() -> int:
        c = Crawler(Resolver())
        try:
            urls = [d.url async for d in c.crawl(Goal(start=httpserver.url_for("/"), max_pages=10))]
            return sum(1 for u in urls if "article" in u)
        finally:
            await c.aclose()

    assert _run(go()) == 1  # both article URLs share a canonical -> one result


def test_parse_robots_rules_and_sitemaps() -> None:
    text = ("User-agent: *\n"
            "Disallow: /private\n"
            "Allow: /private/ok\n"
            "Sitemap: https://ex.com/sitemap.xml\n")
    rob = parse_robots(text)
    assert rob.sitemaps == ["https://ex.com/sitemap.xml"]
    assert rob.allowed("https://ex.com/public")
    assert not rob.allowed("https://ex.com/private/secret")
    assert rob.allowed("https://ex.com/private/ok")  # longer Allow wins


def test_crawl_seeds_from_sitemap(httpserver: HTTPServer) -> None:
    sm = (f"<urlset><url><loc>{httpserver.url_for('/one')}</loc></url>"
          f"<url><loc>{httpserver.url_for('/two')}</loc></url></urlset>").encode()
    httpserver.expect_request("/sitemap.xml").respond_with_data(sm, content_type="application/xml")
    httpserver.expect_request("/one").respond_with_data(b"<p>1</p>", content_type="text/html")
    httpserver.expect_request("/two").respond_with_data(b"<p>2</p>", content_type="text/html")

    async def go() -> list[str]:
        r = Resolver()
        return await sitemap_urls(r, httpserver.url_for("/"))

    urls = _run(go())
    assert urls == [httpserver.url_for("/one"), httpserver.url_for("/two")]


def test_crawl_respects_robots_disallow(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/robots.txt").respond_with_data(
        b"User-agent: *\nDisallow: /secret\n", content_type="text/plain")
    httpserver.expect_request("/").respond_with_data(
        b"<a href='/ok'>ok</a><a href='/secret/x'>no</a>", content_type="text/html")
    httpserver.expect_request("/ok").respond_with_data(b"<p>ok</p>", content_type="text/html")

    async def go() -> list[str]:
        c = Crawler(Resolver())
        try:
            return [d.url async for d in c.crawl(
                Goal(start=httpserver.url_for("/"), max_pages=10, respect_robots=True))]
        finally:
            await c.aclose()

    urls = _run(go())
    assert any("/ok" in u for u in urls) and not any("secret" in u for u in urls)


def test_robots_honours_wildcards_and_longest_match() -> None:
    rob = parse_robots(
        "User-agent: *\n"
        "Disallow: /private\n"
        "Allow: /private/ok\n"      # longer Allow overrides the Disallow (RFC 9309, not first-match)
        "Disallow: /*.pdf$\n"       # wildcard + end-anchor
        "Disallow: /a/*/secret\n"   # mid-path wildcard
    )
    assert rob.allowed("https://ex.com/public")
    assert not rob.allowed("https://ex.com/private/secret")
    assert rob.allowed("https://ex.com/private/ok")           # longest-match Allow wins
    assert not rob.allowed("https://ex.com/report.pdf")       # /*.pdf$ wildcard honoured
    assert rob.allowed("https://ex.com/report.pdf?x=1")       # $ anchors end -> query not blocked
    assert not rob.allowed("https://ex.com/a/b/secret")       # mid-path * honoured
