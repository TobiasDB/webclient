"""web.agent tests -- the ONE bounded loop's interrupt/resume."""

from __future__ import annotations

import asyncio

from web.onboard.agent import Ask, BoundedLoop


def _run(coro):
    return asyncio.run(coro)


def test_bounded_loop_interrupt_and_resume() -> None:
    # a loop that asks for a human decision on the first round, then finishes
    asked = {"n": 0}

    def decide(obs: int):  # type: ignore[no-untyped-def]
        if asked["n"] == 0:
            asked["n"] += 1
            return Ask(reason="which one?", options=["stop"])
        return "stop"  # terminal

    loop: BoundedLoop = BoundedLoop(
        observe=lambda s: 0,
        decide=decide,
        apply=lambda s, d: s.append(1),
        done=lambda d: d == "stop",
        max_rounds=5,
    )
    state: list[int] = []
    v = _run(loop.arun(state))
    assert v.reason == "waiting" and v.ask is not None and v.ask.reason == "which one?"
    v2 = _run(
        loop.resume("go")
    )  # the human answers with a (non-terminal) decision -> applied, then continues
    assert v2.reason == "done" and state == [
        1
    ]  # the resumed decision ran once, then the loop finished
