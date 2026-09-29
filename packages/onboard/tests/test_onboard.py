"""web.onboard test -- the whole stack end to end: resolve -> crawl -> agent(+llm) -> dataset."""

from __future__ import annotations

import asyncio

from pytest_httpserver import HTTPServer
from web.onboard import onboard

from web.resolve import Resolver


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
        b"<div class='row'><span class='n'>Alice</span></div><a href='/b'>b</a>",
        content_type="text/html",
    )
    httpserver.expect_request("/b").respond_with_data(
        b"<div class='row'><span class='n'>Bob</span></div>", content_type="text/html"
    )

    async def go():  # type: ignore[no-untyped-def]
        r = Resolver()
        try:
            return await onboard(
                "each person's name",
                httpserver.url_for("/a"),
                resolver=r,
                llm=_StubLlm(),
                max_pages=5,
            )
        finally:
            await r.aclose()

    result = _run(go())
    assert result.pages == 2 and result.selection is not None and result.selection.row == ".row"
    # the ONE authored Selection was applied across BOTH crawled pages and aggregated
    assert sorted(str(row["n"]) for row in result.rows) == ["Alice", "Bob"]
    assert all("_source" in row for row in result.rows)  # each row tagged with its source page


from web.onboard import AnthropicLlm, llm_driver  # noqa: E402

# -- the LLM tier now lives in onboard: llm_driver (bridges Llm -> agent.Driver) + AnthropicLlm --
from web.onboard.agent import Author  # noqa: E402
from web.parse import parse  # noqa: E402

_ROWS = parse(
    b"<ul><li class='row'><span class='t'>A</span><span class='p'>1</span></li>"
    b"<li class='row'><span class='t'>B</span><span class='p'>2</span></li></ul>",
    content_type="text/html",
)


class _AuthorStub:
    async def complete(self, prompt: str) -> str:
        if "Rows extracted so far: []" in prompt:
            return 'Sure: {"row": ".row", "fields": {"t": ".t", "p": ".p"}}'
        return '{"done": true}'


def test_llm_driver_authors_extraction_via_the_loop() -> None:
    result = _run(Author(_ROWS, llm_driver(_AuthorStub(), goal="t and p")).run())
    assert result.verdict.reason == "done"
    assert result.rows == [{"t": "A", "p": "1"}, {"t": "B", "p": "2"}]
    assert result.selection is not None and result.selection.row == ".row"


def test_llm_driver_tolerates_an_unparseable_reply() -> None:
    class _Garbage:
        async def complete(self, prompt: str) -> str:
            return "not JSON"

    result = _run(Author(_ROWS, llm_driver(_Garbage(), goal="x")).run())
    assert result.verdict.reason == "done" and result.rows == []  # unparseable -> stop, no crash


def test_llm_driver_prompt_uses_skeleton_and_record_hints() -> None:
    captured: dict[str, str] = {}

    class _CaptureLlm:
        async def complete(self, prompt: str) -> str:
            captured["p"] = prompt
            return '{"done": true}'

    page = parse(
        b"<ul>" + b"<li class='row'><span class='t'>x</span></li>" * 3 + b"</ul>",
        content_type="text/html",
    )  # >= 3 rows so record detection fires
    _run(llm_driver(_CaptureLlm(), goal="rows")(page, []))
    assert "RECORD LIST" in captured["p"] and 'select_all("li.row")' in captured["p"]
    assert "Detected record region(s)" in captured["p"]


def test_anthropic_llm_raises_structured_error_on_non_200(
    httpserver: HTTPServer,
) -> None:
    from web.fetch import WebException

    httpserver.expect_request("/v1/messages").respond_with_data(b"nope", status=500)

    async def go() -> str:
        llm = AnthropicLlm(auth="k", base_url=httpserver.url_for(""))
        try:
            await llm.complete("hi")
            return "no-raise"
        except WebException as exc:
            return exc.error.code
        finally:
            await llm.aclose()

    assert _run(go()) == "llm.api"  # non-200 -> structured llm.api error, never a raw httpx leak
