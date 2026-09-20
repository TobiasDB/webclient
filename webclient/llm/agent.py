"""A type-safe, page-scoped agent loop -- observe the current page, pick a typed action,
act, repeat -- bounded, and recorded as a Plan.

The loop is deliberately small and STATELESS in the everything-is-a-Plan sense: it drives
ONE held live page (never opening a new one -- ``goto`` navigates in place), and every
action it takes is one of the document's ordinary interaction ops, so under a recording
session (``with wc.record()``) the whole journey is captured into ``rec.plan`` -- replayable
and condensable like any recorded sequence. The *policy* (the "brain") is any
``Callable[[Observation], Action]`` -- an LLM adapter in production, a plain function in a
test -- and the action set is a typed, discriminated union, so a policy can only return a
well-formed action and the loop can only execute a known one.

    def policy(obs: Observation) -> Action:
        if "load-more" in obs.skeleton and obs.step < 3:
            return Click(selector=".load-more")
        return Done(result=obs.title or "")

    with wc.record() as rec:
        page = rec.ref(url).resolve(browser=True)
        run = drive(page, policy, max_steps=8)
    plan = rec.plan          # the recorded journey -- replay with plan.collect()
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal, Union

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..interface import Document


# -- what the policy sees ----------------------------------------------------
class Observation(BaseModel):
    """A snapshot of the held page handed to the policy each turn: the loop position and
    budget, the current URL/title, and a token-lean ``skeleton`` of the DOM (interactive
    controls marked) -- enough for a policy to choose the next action. ``error`` carries the
    last action's failure message (so the policy can recover) or ``""``."""

    step: int
    max_steps: int
    url: str = ""
    title: str | None = None
    skeleton: str = ""
    error: str = ""


# -- the typed action set (a discriminated union) ----------------------------
class Click(BaseModel):
    action: Literal["click"] = "click"
    selector: str


class Type(BaseModel):
    """Type ``text`` into ``selector`` (the document's ``write``)."""

    action: Literal["type"] = "type"
    selector: str
    text: str


class WaitFor(BaseModel):
    action: Literal["wait_for"] = "wait_for"
    selector: str


class Scroll(BaseModel):
    action: Literal["scroll"] = "scroll"
    selector: str | None = None  # None = scroll to the bottom (reveal lazy content)


class Goto(BaseModel):
    """Navigate the HELD page in place (not a new resolve)."""

    action: Literal["goto"] = "goto"
    url: str


class Done(BaseModel):
    action: Literal["done"] = "done"
    result: str = ""


#: the actions a policy may return -- discriminated on ``action`` so it is exhaustive and
#: a malformed action can't be constructed.
Action = Annotated[
    Union[Click, Type, WaitFor, Scroll, Goto, Done], Field(discriminator="action")
]

#: the "brain": given an :class:`Observation`, choose the next :class:`Action`.
Policy = "Callable[[Observation], Action]"


class AgentRun(BaseModel):
    """The outcome of a :func:`drive` loop. ``reason``: ``done`` (the policy finished),
    ``budget`` (hit ``max_steps``), ``stalled`` (``max_stalls`` no-progress turns in a row),
    or ``error`` (an action raised). The recorded Plan lives on the recording session
    (``rec.plan``), not here -- this is just the loop verdict."""

    done: bool
    reason: Literal["done", "budget", "stalled", "error"]
    steps: int
    result: str = ""
    error: str = ""


def _observe(doc: "Document", step: int, max_steps: int, error: str) -> Observation:
    """Snapshot the current page into an ``Observation`` (url / title / skeleton / step budget /
    any error) -- what the policy model sees to choose the next action."""
    title = doc.title if doc.has_op("title") else None
    skeleton = doc.skeleton() if doc.has_op("skeleton") else ""
    return Observation(
        step=step, max_steps=max_steps, url=getattr(doc, "url", "") or "",
        title=title, skeleton=skeleton, error=error,
    )


def _apply(doc: "Document", act: Any) -> None:
    """Execute one action as an ordinary document interaction (so it records + replays)."""
    if isinstance(act, Click):
        doc.click(act.selector)
    elif isinstance(act, Type):
        doc.write(act.selector, act.text)
    elif isinstance(act, WaitFor):
        doc.wait_for(act.selector)
    elif isinstance(act, Scroll):
        doc.scroll(act.selector)
    elif isinstance(act, Goto):
        doc.goto(act.url)


def drive(
    doc: "Document",
    policy: "Callable[[Observation], Any]",
    *,
    max_steps: int = 20,
    max_stalls: int = 3,
) -> AgentRun:
    """Drive ``doc`` (a held live page) with ``policy`` until it returns :class:`Done`, an
    action fails, or a budget is hit -- BOUNDED by ``max_steps`` and ``max_stalls`` (turns in
    a row that don't change the page). Each action is one of the document's interaction ops,
    so running ``drive`` inside ``with wc.record()`` captures the journey into ``rec.plan``
    (replayable). The loop stays on the one page: :class:`Goto` navigates it in place, never
    opening a new one. Returns the :class:`AgentRun` verdict."""
    stalls = 0
    prev = ""
    error = ""
    for step in range(max_steps):
        obs = _observe(doc, step, max_steps, error)
        act = policy(obs)
        if isinstance(act, Done):
            return AgentRun(done=True, reason="done", steps=step, result=act.result)
        try:
            _apply(doc, act)
            error = ""
        except Exception as exc:  # noqa: BLE001 - surface it to the verdict, don't crash
            return AgentRun(done=False, reason="error", steps=step, error=str(exc))
        current = doc.skeleton() if doc.has_op("skeleton") else ""
        stalls = stalls + 1 if current == prev else 0
        prev = current
        if stalls >= max_stalls:
            return AgentRun(done=False, reason="stalled", steps=step + 1)
    return AgentRun(done=False, reason="budget", steps=max_steps)


__all__ = [
    "Observation", "Action", "Click", "Type", "WaitFor", "Scroll", "Goto", "Done",
    "Policy", "AgentRun", "drive",
]
