"""web.onboard LLM-tier test -- the AnthropicLlm client's structured error handling."""

from __future__ import annotations

import asyncio

from pytest_httpserver import HTTPServer
from web.onboard import AnthropicLlm


def _run(coro):
    return asyncio.run(coro)


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
