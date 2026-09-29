"""web.resolve functional entry -- resolve() one-shot / session / pagination kwarg."""

from __future__ import annotations

import asyncio
from typing import Any

from pytest_httpserver import HTTPServer

from web.resolve import resolve


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_resolve_one_shot_returns_a_document(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/").respond_with_data(b"<title>Home</title>", content_type="text/html")

    async def go() -> str:
        doc = await resolve(httpserver.url_for("/"))   # awaited -> one-shot Document
        return doc.metadata().title or ""

    assert _run(go()) == "Home"


def test_resolve_as_session_reuses_state(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/a").respond_with_data(b"<title>A</title>", content_type="text/html")
    httpserver.expect_request("/b").respond_with_data(b"<title>B</title>", content_type="text/html")

    async def go() -> tuple[str, str]:
        async with resolve(httpserver.url_for("/a")) as session:  # same call, as a session
            a = await session.doc()
            b = await session.resolve(httpserver.url_for("/b"))
            return a.metadata().title or "", b.metadata().title or ""

    assert _run(go()) == ("A", "B")


def test_resolve_pagination_is_a_plain_kwarg(httpserver: HTTPServer) -> None:
    for n in (1, 2):
        httpserver.expect_request("/feed", query_string=f"page={n}").respond_with_data(
            f'<main><article class="row">p{n}</article></main>'.encode(), content_type="text/html")

    async def go() -> list[str]:
        # pagination is just a kwarg -- no Resolver, no middleware assembled by hand
        doc = await resolve(httpserver.url_for("/feed") + "?page=1", paginate="page", max_pages=2)
        return [e.text for e in doc.select_all("article.row")]

    assert _run(go()) == ["p1", "p2"]
