"""web.agent tests -- the loop's interrupt/resume, and extraction authoring with a stub driver."""

from __future__ import annotations

import asyncio

from web.onboard.agent import Ask, Author, BoundedLoop, Done, Selection
from web.parse import parse


def _run(coro):
    return asyncio.run(coro)


_PAGE = parse(
    b"<ul>"
    b"<li class='row'><span class='t'>A</span><span class='p'>1</span></li>"
    b"<li class='row'><span class='t'>B</span><span class='p'>2</span></li>"
    b"</ul>",
    content_type="text/html",
)


def test_bounded_loop_interrupt_and_resume() -> None:
    # a loop that asks for a human decision on the first round, then finishes
    asked = {"n": 0}

    def decide(obs: int):  # type: ignore[no-untyped-def]
        if asked["n"] == 0:
            asked["n"] += 1
            return Ask(reason="which one?", options=["stop"])
        return "stop"  # terminal

    loop: BoundedLoop = BoundedLoop(
        observe=lambda s: 0, decide=decide, apply=lambda s, d: s.append(1),
        done=lambda d: d == "stop", max_rounds=5,
    )
    state: list[int] = []
    v = _run(loop.arun(state))
    assert v.reason == "waiting" and v.ask is not None and v.ask.reason == "which one?"
    v2 = _run(loop.resume("go"))  # the human answers with a (non-terminal) decision -> applied, then continues
    assert v2.reason == "done" and state == [1]  # the resumed decision ran once, then the loop finished


def test_author_refines_then_done() -> None:
    # driver: a wrong selector first (0 rows), then the right one, then Done once it sees rows
    seen = {"round": 0}

    def driver(doc, rows):  # type: ignore[no-untyped-def]
        seen["round"] += 1
        if seen["round"] == 1:
            return Selection(row=".nope", fields={"t": ".t"})       # matches nothing
        if not rows:
            return Selection(row=".row", fields={"t": ".t", "p": ".p"})  # the good one
        return Done()  # rows look good -> finish

    result = _run(Author(_PAGE, driver).run())
    assert result.verdict.reason == "done"
    assert result.rows == [{"t": "A", "p": "1"}, {"t": "B", "p": "2"}]
    assert result.selection is not None and result.selection.row == ".row"


def test_author_asks_then_resumes_with_a_human_selection() -> None:
    def driver(doc, rows):  # type: ignore[no-untyped-def]
        return Ask(reason="which selector?", options=[".row"])  # always defers to a human

    author = Author(_PAGE, driver)
    first = _run(author.run())
    assert first.verdict.reason == "waiting"
    # the human supplies the Selection; the loop applies it and (the driver asks again -> waiting)
    resumed = _run(author.resume(Selection(row=".row", fields={"t": ".t"})))
    assert resumed.rows == [{"t": "A"}, {"t": "B"}]  # the human's selection was applied


# -- the interaction agent: drive a live page toward a goal --
from pytest_httpserver import HTTPServer  # noqa: E402

from web.onboard.agent import Click, Done as _Done, Observation, Type, drive  # noqa: E402
from web.fetch import BrowserFetcher, Request  # noqa: E402


def test_interaction_agent_drives_a_live_page(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/f").respond_with_data(
        b"<html><body><input id='q'>"
        b"<button id='go' onclick=\"document.body.setAttribute('data-done', document.getElementById('q').value)\">go</button>"
        b"</body></html>", content_type="text/html")

    steps = iter([Type(selector="#q", text="hello"), Click(selector="#go"), _Done()])

    def policy(obs: Observation) -> object:
        assert obs.url and obs.skeleton  # the agent observed the live page each round
        return next(steps)

    async def go() -> bytes:
        bf = BrowserFetcher()
        try:
            session = await bf.session()
            await session.goto(Request(url=httpserver.url_for("/f")))
            run = await drive(session, policy, max_rounds=5)
            assert run.verdict.ok  # the policy reached Done
            content = (await session.snapshot()).content
            await session.aclose()
            return content
        finally:
            await bf.aclose()

    assert b'data-done="hello"' in _run(go())  # the typed+clicked actions actually ran


from web.onboard.agent import Scroll  # noqa: E402


def test_drive_does_not_falsely_stall_past_max_stalls(httpserver: HTTPServer) -> None:
    # 5 rounds of a repeatable action then Done: with a constant progress mark this used to
    # FALSELY stall at round 4 (max_stalls). Stall detection is off, so it must reach Done.
    httpserver.expect_request("/p").respond_with_data(
        b"<html><body style='height:5000px'><p>tall</p></body></html>", content_type="text/html")
    steps = iter([Scroll(), Scroll(), Scroll(), Scroll(), Scroll(), _Done()])

    def policy(obs: Observation) -> object:
        return next(steps)

    async def go() -> str:
        bf = BrowserFetcher()
        try:
            session = await bf.session()
            await session.goto(Request(url=httpserver.url_for("/p")))
            run = await drive(session, policy, max_rounds=10)
            await session.aclose()
            return run.verdict.reason
        finally:
            await bf.aclose()

    assert _run(go()) == "done"  # reached Done, not "stalled"
