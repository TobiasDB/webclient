"""The interaction agent -- drive a live page toward a goal, one action at a time.

Where :mod:`.extract` authors a *selector*, this drives *behaviour*: a policy looks at the page and
returns the next action (click / type / scroll / wait / navigate / done), the loop performs it on a
live :class:`~web.fetch.BrowserSession`, and repeats until the policy says ``Done`` or a bound is
hit. It reuses the same :class:`~web.agent.loop.BoundedLoop` (with an async ``observe`` that
snapshots the page), so budget / stall / interrupt-resume come for free. The policy is the brain --
an LLM adapter in production, a plain function in a test.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic import BaseModel

from web.fetch import BrowserSession, Request
from web.parse import parse

from .loop import Ask, BoundedLoop, Done, Verdict


class Observation(BaseModel):
    """What the policy sees each round: the current URL and a token-lean DOM skeleton of the page
    (enough to choose a selector to act on, far cheaper than raw HTML)."""

    url: str
    skeleton: str


class Click(BaseModel):
    selector: str


class Type(BaseModel):
    selector: str
    text: str


class Scroll(BaseModel):
    selector: "str | None" = None  # None -> scroll to the page bottom (trigger lazy load)


class WaitFor(BaseModel):
    selector: str


class Goto(BaseModel):
    url: str


#: the actions the policy may return; the shared ``Done`` (from :mod:`.loop`) ends the loop.
Action = Click | Type | Scroll | WaitFor | Goto | Done
#: the brain: given an :class:`Observation`, choose the next :class:`Action` (sync or async -- an
#: LLM policy is async; the loop awaits it). It may instead return an :class:`Ask` for a human.
Policy = Callable[[Observation], "Action | Ask | Awaitable[Action | Ask]"]


class AgentRun(BaseModel):
    """The result: how many rounds ran and why the loop stopped."""

    verdict: Verdict


async def _observe(session: BrowserSession) -> Observation:
    snap = await session.snapshot()
    doc = parse(snap.content, content_type=snap.headers.get("content-type"), url=snap.url)
    return Observation(url=snap.url, skeleton=doc.skeleton())


async def _apply(session: BrowserSession, action: "Action") -> None:
    if isinstance(action, Click):
        await session.click(action.selector)
    elif isinstance(action, Type):
        await session.type(action.selector, action.text)
    elif isinstance(action, Scroll):
        await session.scroll(action.selector)
    elif isinstance(action, WaitFor):
        await session.wait_for(action.selector)
    elif isinstance(action, Goto):
        await session.goto(Request(url=action.url))
    # Done never reaches apply -- done() catches it first


async def drive(session: BrowserSession, policy: Policy, *, max_rounds: int = 20) -> AgentRun:
    """Drive ``session`` with ``policy`` until it returns ``Done`` (or a bound / an :class:`Ask`).
    The session owns its page; this only acts on it."""
    loop: "BoundedLoop[BrowserSession, Observation, Action]" = BoundedLoop(
        observe=_observe,
        decide=policy,
        apply=_apply,
        done=lambda a: isinstance(a, Done),
        progress=lambda s: None,  # progress/stall detection is per-goal; left to the caller
        max_rounds=max_rounds,
    )
    return AgentRun(verdict=await loop.arun(session))


__all__ = ["Observation", "Click", "Type", "Scroll", "WaitFor", "Goto", "Action",
           "Policy", "AgentRun", "drive"]
