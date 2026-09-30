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


def test_conversation_sends_the_opening_once_cache_marked_and_budget_caps() -> None:
    # The author/evaluate loops run over a CONVERSATION: the big opening (skeleton + guide) is sent
    # ONCE, cache-marked, and every later turn re-sends it byte-identical (a prompt-cache hit) plus a
    # short follow-up. The shared Budget is charged per turn (cache tokens included) and REFUSES a
    # turn before any request once the cap is reached.
    import asyncio
    import json

    import httpx
    from web.onboard.llm import (
        AnthropicLlm,
        Budget,
        BudgetExceeded,
        Conversation,
        Conversational,
        Pricing,
    )
    from web.onboard.shim import ClaudeShim

    bodies: "list[dict[str, object]]" = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        n = len(bodies)
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": f"reply-{n}"}],
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 10,
                    "cache_read_input_tokens": 0 if n == 1 else 900,
                    "cache_creation_input_tokens": 900 if n == 1 else 0,
                },
            },
        )

    async def go() -> None:
        budget = Budget(max_usd=0.003)
        llm = AnthropicLlm(
            auth="k",
            transport=httpx.MockTransport(handler),
            budget=budget,
            pricing=Pricing(input=1.0, output=1.0, cache_read=0.1, cache_write=1.25),
        )
        assert isinstance(llm, Conversational)  # the author's conversation seam
        assert not isinstance(ClaudeShim(), Conversational)  # claude -p is stateless -> fallback
        conv = llm.conversation()
        assert isinstance(conv, Conversation)
        assert await conv.send("BIG OPENING: skeleton+guide") == "reply-1"
        assert await conv.send("short follow-up") == "reply-2"
        m1, m2 = bodies[0]["messages"], bodies[1]["messages"]
        assert isinstance(m1, list) and isinstance(m2, list)
        opening = m1[0]["content"][0]
        assert opening["cache_control"] == {"type": "ephemeral"}  # the opening is cache-marked
        assert m2[0] == m1[0]  # turn 2 re-sends the IDENTICAL opening -> a cache hit, not a resend
        assert [m["role"] for m in m2] == ["user", "assistant", "user"]
        assert isinstance(m2[2]["content"], str)  # the follow-up is a short plain turn
        assert (
            budget.calls == 2 and budget.usage.cache_read == 900 and budget.usage.cache_write == 900
        )
        assert abs(budget.spent_usd - 0.003235) < 1e-9  # cache tokens are BILLED, not free
        try:
            await conv.send("one more")
        except BudgetExceeded as exc:
            assert exc.limit_usd == 0.003 and len(bodies) == 2  # refused BEFORE a 3rd request
        else:
            raise AssertionError("the budget cap did not stop the third turn")
        await llm.aclose()

    asyncio.run(go())


def test_budget_from_env_reads_the_cap(monkeypatch: "pytest.MonkeyPatch") -> None:
    import pytest  # noqa: F401 - the fixture type
    from web.onboard.llm import Budget

    monkeypatch.setenv("WEB_LLM_BUDGET", "1.5")
    assert Budget.from_env().max_usd == 1.5
    monkeypatch.setenv("WEB_LLM_BUDGET", "nope")
    assert Budget.from_env().max_usd is None  # unparsable -> uncapped, never a crash
    monkeypatch.delenv("WEB_LLM_BUDGET")
    assert Budget.from_env().max_usd is None


def test_prompts_render_from_package_data_and_clip_to_budget() -> None:
    # The LLM steps' prompts are DATA (`prompts/*.md` string.Templates): each renders with its
    # placeholder set, a MISSING placeholder fails loudly (never a half-filled prompt), and every
    # page-derived input is clipped to a hard char budget, trimming where the content type is least
    # useful (head / centre / both ends) and noting the trim.
    from web.onboard.prompts import MAX_SKELETON_CHARS, clip, render_prompt

    sets = {
        "pick_edges": dict(entity="Acme", description="d", fields_line="", listing="0. http://a"),
        "verify_seeds": dict(entity="Acme", description="d", fields_line="", seeds="0. http://a"),
        "select_candidates": dict(description="d", fields_line="", pages_json="[]"),
        "evaluate_candidate": dict(
            description="d",
            fields_line="",
            candidate_url="http://a",
            flag_map_json="{}",
            endpoints_json="[]",
            skeleton="<ul>",
            exit_condition="",
        ),
        "write_query": dict(
            guide="G",
            description="d",
            fields_line="",
            pager="",
            skeleton="<ul>",
            hints="",
            recency="",
        ),
    }
    for name, variables in sets.items():
        out = render_prompt(name, **variables)
        assert out and "$" not in out, name  # fully rendered, no unresolved placeholder
    assert "Acme's own website" in render_prompt("pick_edges", **sets["pick_edges"])
    try:
        render_prompt("pick_edges", entity="Acme")
    except KeyError:
        pass
    else:
        raise AssertionError("a missing placeholder must raise, not ship a half-filled prompt")
    text = "H" * 100 + "M" * 100 + "T" * 100
    assert clip(text, 60).startswith("HHHHH") and "trimmed" in clip(text, 60)  # head
    assert clip(text, 60, kind="html").splitlines()[1].startswith("MMMMM")  # the centre
    json_clip = clip(text, 60, kind="json")
    assert json_clip.startswith("HHHHH") and json_clip.endswith("TTTTT")  # both ends
    assert clip("short", 60) == "short" and MAX_SKELETON_CHARS == 16_000  # no-op below budget
