"""The LOCATE loop (roadmap N9): a crawl with a goal -- keep expanding the frontier until a
page satisfies ``until`` (a predicate over the retained page projection), then stop and
report what was found. The onboarding pipeline's "find the dataset" stage is this loop
with an LLM driver; ``wc.locate(seeds, until=...)`` is the general form.

Built ON the crawl (the frontier / scope / dedup / robots machinery is unchanged) and ON
:class:`~webclient.loop.BoundedLoop` (rounds, budget, stall detection, LoopEvents, Ask
checkpoints): a round is one crawl step whose picks come from the crawl's driver.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from pydantic import BaseModel

from ...kernel.loop import Ask, BoundedLoop, LoopVerdict

if TYPE_CHECKING:
    from . import Crawl

__all__ = ["LocateResult", "locate"]


class LocateResult(BaseModel):
    """What a locate loop found: the matching page projections (``found``), how many rounds
    it took, and the loop verdict (``done`` = found; ``budget`` / ``stalled`` = exhausted)."""

    found: list[Any] = []
    rounds: int = 0
    reason: str = ""
    pages: int = 0


def locate(
    crawl: "Crawl[Any]",
    until: "Callable[[Any], bool]",
    *,
    max_rounds: "int | None" = None,
    stop_on_first: bool = True,
) -> LocateResult:
    """Drive ``crawl`` round by round until a retained page satisfies ``until``. Each round
    steps the crawl with its driver's picks (best-first, a custom driver, or an :class:`Ask`
    checkpoint -- then the verdict is ``waiting`` and ``crawl.resume(picks)`` continues by
    hand); every newly retained page is tested. ``stop_on_first`` stops at the first match,
    else the loop runs to the crawl's budget collecting every match."""
    found: list[Any] = []
    seen = 0

    def observe(c: "Crawl[Any]", i: int, err: str) -> dict[str, Any]:
        return {"round": i, "pages": len(c.pages), "frontier": len(c.frontier)}

    def decide(obs: dict[str, Any]) -> Any:
        if crawl.done or crawl.pending is not None:
            return "stop"
        return "step"

    def done_result(decision: Any) -> "str | None":
        if decision == "stop":
            return "found" if found else "exhausted"
        return None

    def apply(c: "Crawl[Any]", decision: Any) -> None:
        nonlocal seen
        c.dispatch("step")
        if c.pending is not None:
            return
        new = list(c.pages)[seen:]
        seen = len(c.pages)
        for page in new:
            try:
                hit = bool(until(page))
            except Exception:  # noqa: BLE001 - a predicate error never aborts the search
                hit = False
            if hit:
                found.append(page)

    def progress(c: "Crawl[Any]") -> Any:
        return (len(c.pages), len(c.frontier), len(found))

    loop: "BoundedLoop[Crawl[Any], dict[str, Any], Any]" = BoundedLoop(
        observe=observe,
        decide=lambda obs: ("stop" if (found and stop_on_first) else decide(obs)),
        done_result=done_result, apply=apply, progress=progress,
        max_rounds=max_rounds, name="locate", bus=getattr(crawl._client, "bus", None),
    )
    verdict: LoopVerdict = loop.run(crawl)
    if crawl.pending is not None:
        verdict = LoopVerdict(done=False, reason="waiting", rounds=verdict.rounds, ask=crawl.pending)
    return LocateResult(found=found, rounds=verdict.rounds, reason=verdict.result or verdict.reason,
                        pages=len(crawl.pages))
