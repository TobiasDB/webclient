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
        # max_retries=0: this tests the STRUCTURED error, not the 5xx retry policy (tested elsewhere)
        llm = AnthropicLlm(auth="k", base_url=httpserver.url_for(""), max_retries=0)
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
        assert isinstance(
            ClaudeShim(), Conversational
        )  # one persistent claude -p process per conversation
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
    # The stages' prompts are DATA (`pipeline/prompts/*.md` string.Templates): each renders with
    # its placeholder set, a MISSING placeholder fails loudly (never a half-filled prompt), and every
    # page-derived input is clipped to a hard char budget, trimming where the content type is least
    # useful (head / centre / both ends) and noting the trim.
    from web.onboard.pipeline.ask import render
    from web.onboard.prompts import clip

    sets = {
        "review_search": dict(goal="g", scope="", results="1. [2] http://a", note=""),
        "review_candidate": dict(
            goal="g", scope="", url="http://a", records="3", skeleton="<ul>", note=""
        ),
        "review_location": dict(goal="g", scope="", source="url: a", note=""),
        "author_extract": dict(
            goal="g",
            schema="- a (string): x",
            kind="HTML record",
            records="li",
            count="3",
            structure="<li>",
            hint="",
            note="",
        ),
        "author_review": dict(
            goal="g", schema="- a", count="3", expected="1-5", optional="none", rows="[]", note=""
        ),
    }
    for name, variables in sets.items():
        out = render(name, **variables)
        assert out and "$" not in out and len(out) < 2500, name  # rendered, tiny, no leftover
    try:
        render("review_search", goal="g")
    except KeyError:
        pass
    else:
        raise AssertionError("a missing placeholder must raise, not ship a half-filled prompt")
    text = "H" * 100 + "M" * 100 + "T" * 100
    assert clip(text, 60).startswith("HHHHH") and "trimmed" in clip(text, 60)  # head
    assert clip(text, 60, kind="html").splitlines()[1].startswith("MMMMM")  # the centre
    json_clip = clip(text, 60, kind="json")
    assert json_clip.startswith("HHHHH") and json_clip.endswith("TTTTT")  # both ends
    assert clip("short", 60) == "short"  # no-op below budget


def test_anthropic_llm_retries_transient_errors_but_not_bad_requests(
    monkeypatch: "pytest.MonkeyPatch",
) -> None:
    # A 5xx / 529 / 429 is retried with backoff (a transient overload is not a dead model); a 400 is
    # not (it is our mistake -- surface it at once). Retries are metered ONCE, on the reply.
    import asyncio

    import httpx
    import pytest  # noqa: F401 - the fixture type
    from web.fetch import WebException
    from web.onboard import llm as llm_mod
    from web.onboard.llm import AnthropicLlm

    monkeypatch.setattr(llm_mod, "_BACKOFF_BASE", 0.0)  # no real sleeping in the test
    seen: "list[int]" = []

    def flaky(_req: httpx.Request) -> httpx.Response:
        seen.append(1)
        if len(seen) == 1:
            return httpx.Response(500, text="overloaded")
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    def bad(_req: httpx.Request) -> httpx.Response:
        seen.append(1)
        return httpx.Response(400, text="bad request")

    async def go() -> None:
        llm = AnthropicLlm(auth="k", transport=httpx.MockTransport(flaky), max_retries=2)
        assert await llm.complete("x") == "ok" and llm.calls == 1 and len(seen) == 2  # one retry
        await llm.aclose()
        seen.clear()
        llm2 = AnthropicLlm(auth="k", transport=httpx.MockTransport(bad), max_retries=2)
        try:
            await llm2.complete("x")
        except WebException as exc:
            assert "HTTP 400" in str(exc) and len(seen) == 1  # no retry on a 400
        else:
            raise AssertionError("a 400 must raise, not be retried into success")
        await llm2.aclose()

    asyncio.run(go())


def test_claude_shim_retries_a_timed_out_call() -> None:
    # A `claude -p` call that times out / crashes is retried with backoff (WEB_LLM_RETRIES) -- one
    # transient stall no longer aborts a whole authoring run; a non-shim error is not retried.
    import asyncio

    from web.fetch import WebException, err
    from web.onboard.shim import ClaudeShim

    shim = ClaudeShim(max_retries=2)
    calls = {"n": 0}

    async def flaky(prompt: str) -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise WebException(err("llm.shim", "claude -p timed out after 1.0s"))
        return "the reply"

    shim._once = flaky  # type: ignore[method-assign]
    orig_sleep = asyncio.sleep

    async def no_sleep(_s: float) -> None:
        await orig_sleep(0)

    asyncio.sleep = no_sleep  # type: ignore[assignment]
    try:
        assert asyncio.run(shim.complete("hi")) == "the reply" and calls["n"] == 3
        strict, calls["n"] = ClaudeShim(max_retries=0), 0
        strict._once = flaky  # type: ignore[method-assign]
        try:
            asyncio.run(strict.complete("hi"))
        except WebException as exc:
            assert exc.error.code == "llm.shim" and calls["n"] == 1  # no retries -> one call
        else:
            raise AssertionError("max_retries=0 must raise on the first failure")
    finally:
        asyncio.sleep = orig_sleep  # type: ignore[assignment]


def test_expected_rows_is_a_flexible_guide() -> None:
    # USER: record counts are flawed as a hard rule -- the brief may carry an EXPECTED range of rows
    # and every stage treats it as a guide (a note), never a veto.
    from web.onboard import Brief
    from web.onboard.models import in_range, parse_range

    assert parse_range("10-50") == (10, 50)
    assert parse_range("~20") == (10, 40)
    assert parse_range(">=5") == (5, 10**9) and parse_range("5+") == (5, 10**9)
    assert parse_range("<200") == (0, 199) and parse_range("<=200") == (0, 200)
    assert parse_range("40") == (30, 51)
    assert parse_range("") is None and parse_range("lots") is None
    assert in_range(25, (10, 50)) is None and in_range(25, None) is None
    assert "BELOW" in (in_range(3, (10, 50)) or "") and "ABOVE" in (in_range(200, (10, 50)) or "")
    brief = Brief.from_markdown("---\nexpect_rows: 10-50\nschema:\n  - a: x\n---\ng")
    assert brief.expected_range() == (10, 50)


def test_claude_shim_conversation_keeps_one_process_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # USER: "start one Claude process and build a conversation API on top of it". A conversation
    # feeds turns to ONE `claude -p --input-format stream-json` process (the fake CLI numbers its
    # turns), meters each turn, and -- when the process dies mid-turn -- restarts it on the next
    # send with a catch-up of the exchange so far. The shim is Conversational, so the author loop
    # picks it up.
    import asyncio
    import os
    from pathlib import Path

    from web.fetch import WebException
    from web.onboard.llm import Conversational
    from web.onboard.shim import ClaudeShim

    fake = Path(__file__).parent / "fake_claude"
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ.get('PATH', '')}")
    shim = ClaudeShim(model="haiku", timeout=10)
    assert isinstance(shim, Conversational)

    async def go() -> None:
        conv = shim.conversation()
        assert await conv.send("opening prompt\nfirst") == "turn1:first"
        assert await conv.send("second") == "turn2:second"  # the SAME process: turn 2
        assert shim.calls == 2 and shim.usage.cache_read == 7  # metered per turn
        try:
            await conv.send("please CRASH now")
        except WebException as exc:
            assert exc.error.code == "llm.shim" and "exited" in exc.error.message
        else:
            raise AssertionError("a process that dies mid-turn must raise")
        reply = await conv.send("third")  # a fresh process, caught up: its turn 1 again
        assert reply.startswith("turn1:") and reply.endswith("third")
        assert await shim.complete("one\nshot") == "oneshot:shot"  # the one-shot form still works
        await shim.aclose()

    asyncio.run(go())
