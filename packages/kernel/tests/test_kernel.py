"""web.kernel unit tests -- the layer in isolation (it imports nothing but pydantic)."""

from __future__ import annotations

import asyncio

from web.kernel import BoundedLoop, Event, EventBus, Verdict, WebError, WebException, err


def test_weberror_is_data_and_exception_carries_it() -> None:
    e = err("http.timeout", "no response", url="http://x/")
    assert e.code == "http.timeout" and e.detail["url"] == "http://x/"
    assert str(e) == "http.timeout: no response"
    exc = WebException(e)
    assert exc.error is e
    # a bare-code form also works
    assert WebException("parse.not_html").error == WebError(code="parse.not_html")


def test_bus_delivers_by_prefix_and_swallows_handler_errors() -> None:
    bus = EventBus()
    seen: list[str] = []
    bus.subscribe("fetch", lambda ev: seen.append(ev.topic))
    bus.subscribe("", lambda ev: (_ for _ in ()).throw(RuntimeError("boom")))  # never breaks emit
    all_seen: list[str] = []
    sub = bus.subscribe("", lambda ev: all_seen.append(ev.topic))

    bus.publish(Event(topic="fetch.retry"))
    bus.publish(Event(topic="parse"))
    assert seen == ["fetch.retry"]  # prefix match only
    assert all_seen == ["fetch.retry", "parse"]  # "" sees everything
    sub()  # unsubscribe
    bus.publish(Event(topic="fetch"))
    assert all_seen == ["fetch.retry", "parse"]  # no longer delivered


def test_bounded_loop_terminates_on_done_budget_and_stall() -> None:
    # done: decision is terminal once the counter reaches 3
    loop = BoundedLoop[list[int], int, str](
        observe=lambda s: len(s),
        decide=lambda n: "stop" if n >= 3 else "go",
        apply=lambda s, d: s.append(1),
        done=lambda d: d == "stop",
    )
    v = loop.run([])
    assert v == Verdict(reason="done", rounds=3)

    # budget: never terminal, capped by max_rounds
    budget = BoundedLoop[list[int], int, str](
        observe=lambda s: 0, decide=lambda n: "go", apply=lambda s, d: s.append(1),
        done=lambda d: False, max_rounds=5,
    )
    assert budget.run([]).reason == "budget"

    # stalled: progress never changes
    stalled = BoundedLoop[list[int], int, str](
        observe=lambda s: 0, decide=lambda n: "go", apply=lambda s, d: None,
        done=lambda d: False, progress=lambda s: 0, max_stalls=2, max_rounds=99,
    )
    sv = stalled.run([])
    # round 1 sets the progress baseline; rounds 2 and 3 are the two no-progress rounds.
    assert sv.reason == "stalled" and sv.rounds == 3


def test_bounded_loop_arun_awaits_async_apply() -> None:
    async def main() -> Verdict:
        async def apply(s: list[int], d: str) -> None:
            await asyncio.sleep(0)
            s.append(1)

        loop = BoundedLoop[list[int], int, str](
            observe=lambda s: len(s), decide=lambda n: "stop" if n >= 2 else "go",
            apply=apply, done=lambda d: d == "stop",
        )
        return await loop.arun([])

    v = asyncio.run(main())
    assert v == Verdict(reason="done", rounds=2)


def test_apply_error_is_a_verdict_not_a_raise() -> None:
    loop = BoundedLoop[list[int], int, str](
        observe=lambda s: 0, decide=lambda n: "go",
        apply=lambda s, d: (_ for _ in ()).throw(ValueError("nope")),
        done=lambda d: False,
    )
    v = loop.run([])
    assert v.reason == "error" and "nope" in v.error
