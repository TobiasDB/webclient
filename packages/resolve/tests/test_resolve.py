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
        r = Resolver()
        try:
            return await r.resolve(Request(url=httpserver.url_for("/p")))
        finally:
            await r.aclose()

    doc = _run(go())
    assert doc.kind == "html" and doc.select_all("h1")[0].text == "hi"


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
        r = Resolver(ladder=(fetcher,), retry=retry(max_attempts=3, backoff=0.0))
        return await r.resolve(Request(url="https://x/"))

    doc = _run(go())
    assert fetcher.calls == 3 and doc.select_all("p")[0].text == "ok"  # 2 failures + 1 success
    assert doc.select_all("p")[0].text == "ok"


def test_retry_gives_up_and_returns_the_last_document() -> None:
    fetcher = _FlakyFetcher(fail=99)

    async def go() -> Document:
        r = Resolver(ladder=(fetcher,), retry=retry(max_attempts=2, backoff=0.0))
        return await r.resolve(Request(url="https://x/"))

    doc = _run(go())
    assert fetcher.calls == 2 and doc.select("p") is None  # gave up -> empty content


def test_rate_limit_spaces_same_host_requests() -> None:
    fetcher = _FlakyFetcher(fail=0)

    async def go() -> float:
        r = Resolver(ladder=(fetcher,), rate_limit=0.05)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await r.resolve(Request(url="https://x/a"))
        await r.resolve(Request(url="https://x/b"))
        return loop.time() - t0

    elapsed = _run(go())
    assert elapsed >= 0.05  # the second request waited for the host's slot


# -- signals: clean, standalone detector functions over a Document (a sub-part of resolve) --

from web.parse import parse  # noqa: E402
from web.resolve import Signal, anti_bot, login_wall, pagination, spa  # noqa: E402


def _d(html: bytes):  # a parsed Document from bytes
    return parse(html, content_type="text/html")


def test_spa_signal_on_a_client_rendered_shell() -> None:
    s = spa(_d(b"<html><body><div id='root'></div><script src='/app.js'></script></body></html>"))
    assert isinstance(s, Signal) and s.name == "spa"
    # a server-rendered page with real text does NOT fire spa
    assert spa(_d(b"<html><body><div id='root'>" + b"content " * 60 + b"</div></body></html>")) is None


def test_login_and_pagination_detectors() -> None:
    assert login_wall(_d(b"<form><input type='password'></form>")) is not None
    assert pagination(_d(b"<a rel='next' href='/2'>next</a>")) is not None


def test_anti_bot_reads_content_markers() -> None:
    assert anti_bot(_d(b"<html><body>Please verify you are human (captcha)</body></html>")) is not None
    assert anti_bot(_d(b"<html><body>normal page</body></html>")) is None
