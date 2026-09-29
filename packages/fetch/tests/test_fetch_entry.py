"""web.fetch functional entry -- fetch() one-shot / session, and Profile inheritance."""

from __future__ import annotations

import asyncio
from typing import Any

from pytest_httpserver import HTTPServer

from web.fetch import Profile, Request, fetch


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_fetch_one_shot_returns_a_snapshot(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/").respond_with_data(b"hello", content_type="text/plain")

    async def go() -> tuple[int, bytes]:
        snap = await fetch(httpserver.url_for("/"))   # awaited -> one-shot Snapshot
        return snap.status, snap.content

    status, content = _run(go())
    assert status == 200 and content == b"hello"


def test_fetch_as_session_does_multiple_hops(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/a").respond_with_data(b"A")
    httpserver.expect_request("/b").respond_with_data(b"B")

    async def go() -> tuple[bytes, bytes]:
        async with fetch(httpserver.url_for("/a")) as session:   # same call, as a session
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
