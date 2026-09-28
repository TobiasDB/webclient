"""web.onboard test -- the whole stack end to end: resolve -> crawl -> agent(+llm) -> dataset."""

from __future__ import annotations

import asyncio

from pytest_httpserver import HTTPServer

from web.resolve import Resolver
from web.onboard import onboard


def _run(coro):
    return asyncio.run(coro)


class _StubLlm:
    """Authors the row selector (once, while there are no rows), then signals done."""

    async def complete(self, prompt: str) -> str:
        if "Rows extracted so far: []" in prompt:
            return 'Use: {"row": ".row", "fields": {"n": ".n"}}'
        return '{"done": true}'


def test_onboard_crawls_authors_once_and_aggregates(httpserver: HTTPServer) -> None:
    # two item pages, same shape, linked so the crawl reaches both
    httpserver.expect_request("/a").respond_with_data(
        b"<div class='row'><span class='n'>Alice</span></div><a href='/b'>b</a>", content_type="text/html")
    httpserver.expect_request("/b").respond_with_data(
        b"<div class='row'><span class='n'>Bob</span></div>", content_type="text/html")

    async def go():  # type: ignore[no-untyped-def]
        r = Resolver()
        try:
            return await onboard("each person's name", httpserver.url_for("/a"),
                                 resolver=r, llm=_StubLlm(), max_pages=5)
        finally:
            await r.aclose()

    result = _run(go())
    assert result.pages == 2 and result.selection is not None and result.selection.row == ".row"
    # the ONE authored Selection was applied across BOTH crawled pages and aggregated
    assert sorted(str(row["n"]) for row in result.rows) == ["Alice", "Bob"]
    assert all("_source" in row for row in result.rows)  # each row tagged with its source page
