"""web.fetch functional entry -- fetch() one-shot / session, and Profile inheritance."""

from __future__ import annotations

import asyncio
from typing import Any

from pytest_httpserver import HTTPServer
from web.fetch import BrowserFetcher, ClientPool, Profile, Request, fetch


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_pool_leases_one_shared_backend_per_profile() -> None:
    async def go() -> tuple[bool, bool]:
        async with ClientPool() as pool:
            a = pool.lease(Profile())
            b = pool.lease(Profile())  # same profile -> SAME backend (reused)
            c = pool.lease(Profile(headers={"x": "y"}))  # different profile -> its own backend
            return a is b, a is c

    same, different = _run(go())
    assert same is True and different is False


def test_browser_profile_carries_an_executable_path() -> None:
    base = Profile(browser=True)
    pinned = base.with_(executable_path="/opt/chromium/chrome")
    assert pinned.executable_path == "/opt/chromium/chrome"
    assert base.executable_path is None  # inherited copy, base unchanged
    assert base.key() != pinned.key()  # a different binary -> its own backend
    fetcher = pinned.fetcher()
    assert isinstance(fetcher, BrowserFetcher) and fetcher._executable == "/opt/chromium/chrome"


def test_profile_realness_fields_build_the_right_browser() -> None:
    # the browser-realness rungs: headless bundled -> headed bundled -> real Chrome channel.
    from web.fetch import profiles as fp

    assert fp.BROWSER.headless and fp.BROWSER.channel == "chromium"
    assert not fp.HEADED_BROWSER.headless  # a real window
    assert not fp.REAL_CHROME.headless and fp.REAL_CHROME.channel == "chrome"  # genuine Chrome
    # headless / channel are part of the pool identity, so each rung gets its own backend
    assert fp.BROWSER.key() != fp.HEADED_BROWSER.key() != fp.REAL_CHROME.key()
    built = fp.REAL_CHROME.fetcher()
    assert isinstance(built, BrowserFetcher)
    assert built._channel == "chrome" and built._headless is False


def test_fetch_reuses_the_pooled_backend_across_calls(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/").respond_with_data(b"hi")

    async def go() -> tuple[bytes, bytes, int]:
        async with ClientPool() as pool:
            first = await fetch(httpserver.url_for("/"), pool=pool)  # leases + keeps the backend
            second = await fetch(httpserver.url_for("/"), pool=pool)  # reuses it (not relaunched)
            return first.content, second.content, len(pool._backends)

    a, b, n = _run(go())
    assert a == b == b"hi" and n == 1  # one shared backend served both


def test_fetch_one_shot_returns_a_snapshot(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/").respond_with_data(b"hello", content_type="text/plain")

    async def go() -> tuple[int, bytes]:
        snap = await fetch(httpserver.url_for("/"))  # awaited -> one-shot Snapshot
        return snap.status, snap.content

    status, content = _run(go())
    assert status == 200 and content == b"hello"


def test_fetch_as_session_does_multiple_hops(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/a").respond_with_data(b"A")
    httpserver.expect_request("/b").respond_with_data(b"B")

    async def go() -> tuple[bytes, bytes]:
        async with fetch(httpserver.url_for("/a")) as session:  # same call, as a session
            first = await session.fetch(Request(url=httpserver.url_for("/a")))
            second = await session.fetch(Request(url=httpserver.url_for("/b")))
            return first.content, second.content

    assert _run(go()) == (b"A", b"B")


def test_profile_headers_are_sent_and_inheritable(httpserver: HTTPServer) -> None:
    seen: dict[str, str] = {}

    def handler(req: Any) -> Any:
        from werkzeug import Response

        seen["x-app"] = req.headers.get("x-app", "")
        return Response(b"ok")

    httpserver.expect_request("/").respond_with_handler(handler)
    base = Profile(headers={"x-app": "demo"})
    child = base.with_(browser=False)  # inheritance keeps the headers

    async def go() -> None:
        await fetch(httpserver.url_for("/"), profile=child)

    _run(go())
    assert seen["x-app"] == "demo"
