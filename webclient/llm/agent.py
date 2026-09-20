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

from ..core.document.models import IndexedElement
from ..loop import BoundedLoop

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..interface import Document


# -- what the policy sees ----------------------------------------------------
class Observation(BaseModel):
    """A snapshot of the held page handed to the policy each turn: the loop position and
    budget, the current URL/title, a token-lean ``skeleton`` of the DOM, and ``elements`` -- the
    numbered, class-free table of interactive controls (:class:`IndexedElement`) the policy can
    target BY INDEX. ``error`` carries the last action's failure message (so the policy can
    recover) or ``""``."""

    step: int
    max_steps: int
    url: str = ""
    title: str | None = None
    skeleton: str = ""
    elements: list[IndexedElement] = []
    error: str = ""


# -- the typed action set (a discriminated union) ----------------------------
# The target actions accept EITHER a class-free ``index`` into the observation's element table
# (the loop resolves it to a durable selector) OR an explicit ``selector`` -- so a policy can
# reason purely in indexes (the Phase-4 path) yet a selector policy still works (additive).
class Click(BaseModel):
    action: Literal["click"] = "click"
    index: int | None = None
    selector: str = ""


class Type(BaseModel):
    """Type ``text`` into the target element (by ``index`` or ``selector``) -- the document's
    ``write``."""

    action: Literal["type"] = "type"
    index: int | None = None
    selector: str = ""
    text: str


class WaitFor(BaseModel):
    action: Literal["wait_for"] = "wait_for"
    index: int | None = None
    selector: str = ""


class Scroll(BaseModel):
    action: Literal["scroll"] = "scroll"
    index: int | None = None
    selector: str | None = None  # None (and no index) = scroll to the bottom (reveal lazy content)


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
    """Snapshot the current page into an ``Observation`` (url / title / skeleton / the interactive
    element table / step budget / any error) -- what the policy model sees to choose the next
    action. The element table lets the policy target a control by INDEX, class-free."""
    title = doc.title if doc.has_op("title") else None
    skeleton = doc.skeleton() if doc.has_op("skeleton") else ""
    elements = doc.controls() if doc.has_op("controls") else []
    return Observation(
        step=step, max_steps=max_steps, url=getattr(doc, "url", "") or "",
        title=title, skeleton=skeleton, elements=elements, error=error,
    )


def _resolve(act: Any, obs: Observation) -> Any:
    """Turn an INDEX-based action into a selector action using the current observation's element
    table -- the index the agent picked is looked up to its durable selector, so the loop then
    executes and RECORDS a normal ``action + selector``. A selector/no-target action passes
    through. An index with no matching element resolves to no selector, so :func:`_apply` skips
    it (freshness: the element vanished this round -> re-observe next round, don't act blindly)."""
    idx = getattr(act, "index", None)
    if idx is None:
        return act
    el = next((e for e in obs.elements if e.index == idx), None)
    if el is None or not el.selector:
        return act
    return act.model_copy(update={"selector": el.selector})


def _apply(doc: "Document", act: Any) -> None:
    """Execute one action as an ordinary document interaction (so it records + replays). An
    index-based action whose index did not resolve to a selector this round is a NO-OP (the
    element is stale/gone -- skip and re-observe next round rather than act on a stale index)."""
    if getattr(act, "index", None) is not None and not act.selector:
        return  # freshness: an unresolved index -> skip, re-observe next round
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


def _done_result(act: Any) -> "str | None":
    """The interaction loop's terminal check: a :class:`Done` ends the loop with its result;
    any other action keeps it running."""
    return act.result if isinstance(act, Done) else None


def _progress(doc: "Document") -> str:
    """A cheap signature of visible page progress (its skeleton) for stall detection -- two
    identical signatures in a row mean the last action changed nothing."""
    return doc.skeleton() if doc.has_op("skeleton") else ""


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
    opening a new one. The interaction loop is the :class:`~webclient.loop.BoundedLoop` base
    specialised for a live page; returns the :class:`AgentRun` verdict."""
    loop: "BoundedLoop[Document, Observation, Any]" = BoundedLoop(
        observe=lambda d, step, err: _observe(d, step, max_steps, err),
        # the policy may target a control by INDEX; resolve it to a durable selector against the
        # just-observed element table before applying (so the recorded step is a normal selector).
        decide=lambda obs: _resolve(policy(obs), obs),
        done_result=_done_result,
        apply=_apply,
        progress=_progress,
        max_rounds=max_steps,
        max_stalls=max_stalls,
    )
    v = loop.run(doc)
    return AgentRun(
        done=v.done, reason=v.reason, steps=v.rounds, result=v.result, error=v.error
    )


__all__ = [
    "Observation", "Action", "Click", "Type", "WaitFor", "Scroll", "Goto", "Done",
    "Policy", "AgentRun", "drive",
]
