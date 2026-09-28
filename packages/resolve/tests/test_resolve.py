"""web.resolve tests -- orchestration + middleware, against a local httpserver and stub fetchers."""

from __future__ import annotations

import asyncio

from pytest_httpserver import HTTPServer

from web.fetch import HttpFetcher, Request, Snapshot
from web.parse import Document
from web.resolve import Resolver, rate_limit, retry


def _run(coro):
    return asyncio.run(coro)


def test_resolver_fetches_and_parses(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/p").respond_with_data(b"<h1>hi</h1>", content_type="text/html")

    async def go() -> Document:
        r = Resolver(HttpFetcher())
        try:
            return await r.resolve(Request(url=httpserver.url_for("/p")))
        finally:
            await r.aclose()

    doc = _run(go())
    assert doc.kind == "html" and doc.ok and doc.select_all("h1")[0].text == "hi"


class _FlakyFetcher:
    """Fails (transport error) for the first ``fail`` calls, then succeeds -- to exercise retry."""

    def __init__(self, fail: int) -> None:
        self.calls = 0
        self._fail = fail

    async def fetch(self, request: Request) -> Snapshot:
        self.calls += 1
        if self.calls <= self._fail:
            from web.kernel import err

            return Snapshot(request=request, error=err("fetch.transport", "boom"))
        return Snapshot(request=request, status=200, content=b"<p>ok</p>",
                        headers={"content-type": "text/html"})

    async def aclose(self) -> None:
        pass


def test_retry_middleware_recovers_from_transient_failure() -> None:
    fetcher = _FlakyFetcher(fail=2)

    async def go() -> Document:
        r = Resolver(fetcher, middleware=(retry(max_attempts=3, backoff=0.0),))
        return await r.resolve(Request(url="https://x/"))

    doc = _run(go())
    assert doc.ok and fetcher.calls == 3  # 2 failures + 1 success
    assert doc.select_all("p")[0].text == "ok"


def test_retry_gives_up_and_returns_the_last_document() -> None:
    fetcher = _FlakyFetcher(fail=99)

    async def go() -> Document:
        r = Resolver(fetcher, middleware=(retry(max_attempts=2, backoff=0.0),))
        return await r.resolve(Request(url="https://x/"))

    doc = _run(go())
    assert not doc.ok and fetcher.calls == 2 and doc.error is not None


def test_rate_limit_spaces_same_host_requests() -> None:
    fetcher = _FlakyFetcher(fail=0)

    async def go() -> float:
        r = Resolver(fetcher, middleware=(rate_limit(0.05),))
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await r.resolve(Request(url="https://x/a"))
        await r.resolve(Request(url="https://x/b"))
        return loop.time() - t0

    elapsed = _run(go())
    assert elapsed >= 0.05  # the second request waited for the host's slot


# -- signals: purely-functional detectors over a Document (parse stays bytes -> Document) --

from web.parse import parse_bytes  # noqa: E402
from web.resolve import Signal, detect, login_wall, pagination, spa  # noqa: E402


def test_spa_signal_on_a_client_rendered_shell() -> None:
    shell = parse_bytes(
        b"<html><body><div id='root'></div><script src='/app.js'></script></body></html>",
        content_type="text/html",
    )
    s = spa(shell)
    assert isinstance(s, Signal) and s.name == "spa"
    # a server-rendered page with real text does NOT fire spa
    full = parse_bytes(b"<html><body><div id='root'>" + b"content " * 60 + b"</div></body></html>",
                       content_type="text/html")
    assert spa(full) is None


def test_login_and_pagination_detectors_are_pure() -> None:
    login = parse_bytes(b"<form><input type='password'></form>", content_type="text/html")
    assert login_wall(login) is not None and login_wall(login).name == "login_wall"
    paged = parse_bytes(b"<a rel='next' href='/2'>next</a>", content_type="text/html")
    assert pagination(paged) is not None


def test_detect_runs_all_and_collects_fired() -> None:
    doc = parse_bytes(
        b"<html><body><div id='app'></div><script src='/a.js'></script>"
        b"<input type='password'></body></html>", content_type="text/html")
    names = {s.name for s in detect(doc)}
    assert {"spa", "login_wall"} <= names
    # a JSON document fires nothing (detectors guard on kind)
    assert detect(parse_bytes(b'{"a":1}', content_type="application/json")) == []


def test_escalate_renders_a_spa_shell_via_browser(httpserver: HTTPServer) -> None:
    from web.fetch import BrowserFetcher
    from web.resolve import escalate
    # a JS shell: static resolve sees an empty #root and fires spa -> escalate renders it
    httpserver.expect_request("/spa").respond_with_data(
        b"<html><body><div id='root'></div>"
        b"<script>document.getElementById('root').textContent='REND'+'ERED'</script></body></html>",
        content_type="text/html")

    async def go() -> str:
        browser = BrowserFetcher()
        r = Resolver(HttpFetcher(), middleware=(escalate(browser),))
        try:
            doc = await r.resolve(Request(url=httpserver.url_for("/spa")))
            return doc.select_all("#root")[0].text
        finally:
            await r.aclose()
            await browser.aclose()

    assert _run(go()) == "RENDERED"  # the expensive tier ran only because the signal said so


# -- pagination as middleware (beside retry/escalate): link / param / click strategies --

def test_paginate_links_middleware_merges_pages(httpserver: HTTPServer) -> None:
    from web.resolve import paginate_links
    httpserver.expect_request("/p1").respond_with_data(
        b"<li class='row'>a</li><a rel='next' href='/p2'>n</a>", content_type="text/html")
    httpserver.expect_request("/p2").respond_with_data(
        b"<li class='row'>b</li><a rel='next' href='/p3'>n</a>", content_type="text/html")
    httpserver.expect_request("/p3").respond_with_data(b"<li class='row'>c</li>", content_type="text/html")

    async def go() -> list[str]:
        r = Resolver(HttpFetcher(), middleware=(paginate_links(),))
        try:
            doc = await r.resolve(Request(url=httpserver.url_for("/p1")))  # ONE merged Document
            return [e.text for e in doc.select_all(".row")]
        finally:
            await r.aclose()

    assert _run(go()) == ["a", "b", "c"]  # select_all spans all three pages


def test_paginate_param_with_composed_stops(httpserver: HTTPServer) -> None:
    from web.resolve import any_of, first_n, paginate_param, until_empty
    httpserver.expect_request("/items", query_string="page=1").respond_with_data(
        b"<li class='row'>1</li><li class='row'>2</li>", content_type="text/html")
    httpserver.expect_request("/items", query_string="page=2").respond_with_data(
        b"<li class='row'>3</li>", content_type="text/html")
    httpserver.expect_request("/items", query_string="page=3").respond_with_data(
        b"<p>empty</p>", content_type="text/html")

    async def go() -> int:
        # stop on the FIRST of: page empty, OR 10 items collected
        stop = any_of(until_empty(".row"), first_n(10, ".row"))
        r = Resolver(HttpFetcher(), middleware=(paginate_param("page", until=stop),))
        try:
            doc = await r.resolve(Request(url=httpserver.url_for("/items") + "?page=1"))
            return len(doc.select_all(".row"))
        finally:
            await r.aclose()

    assert _run(go()) == 3  # pages 1-2 (2+1 rows); page 3 empty stopped it


def test_first_n_stop_bounds_by_item_budget(httpserver: HTTPServer) -> None:
    from web.resolve import first_n, paginate_param
    for pg in (1, 2, 3, 4):
        httpserver.expect_request("/b", query_string=f"page={pg}").respond_with_data(
            b"<li class='row'>x</li><li class='row'>y</li>", content_type="text/html")

    async def go() -> int:
        r = Resolver(HttpFetcher(), middleware=(paginate_param("page", until=first_n(5, ".row"), max_pages=99),))
        try:
            doc = await r.resolve(Request(url=httpserver.url_for("/b") + "?page=1"))
            return len(doc.select_all(".row"))
        finally:
            await r.aclose()

    # 2 rows/page; stops on the page that crosses 5 -> pages 1,2,3 = 6 rows (not the whole 99)
    assert _run(go()) == 6


def test_paginate_clicks_middleware_load_more(httpserver: HTTPServer) -> None:
    from web.fetch import BrowserFetcher
    from web.resolve import paginate_clicks
    httpserver.expect_request("/lm").respond_with_data(
        b"<html><body><ul id='list'><li class='row'>1</li></ul>"
        b"<button id='more' onclick=\""
        b"var n=document.querySelectorAll('.row').length+1;"
        b"var li=document.createElement('li');li.className='row';li.textContent=n;"
        b"document.getElementById('list').appendChild(li);if(n>=3)this.remove();\">more</button></body></html>",
        content_type="text/html")

    async def go() -> int:
        browser = BrowserFetcher()
        r = Resolver(HttpFetcher(), middleware=(paginate_clicks(browser, "#more"),))
        try:
            doc = await r.resolve(Request(url=httpserver.url_for("/lm")))
            return len(doc.select_all(".row"))
        finally:
            await r.aclose()
            await browser.aclose()

    assert _run(go()) == 3  # clicked Load-more until the button removed itself
