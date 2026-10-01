"""The ordered stage table and ``run`` -- resume at the first empty slot, save after each stage."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel
from web.fetch import emit

from ..llm import ReasonEvent
from .ask import Context
from .stages import (
    author_extract,
    author_resolve,
    author_review,
    crawl,
    expand,
    review_candidate,
    review_location,
    review_search,
    search,
)
from .state import STAGE_NAMES, Onboarding, StageLog, now


@dataclass(frozen=True)
class Stage:
    name: str
    run: "Callable[[Onboarding, Context], Awaitable[BaseModel]]"


STAGES: "tuple[Stage, ...]" = (
    Stage("search", search.run),
    Stage("review_search", review_search.run),
    Stage("crawl", crawl.run),
    Stage("review_candidate", review_candidate.run),
    Stage("expand", expand.run),
    Stage("review_location", review_location.run),
    Stage("author_resolve", author_resolve.run),
    Stage("author_extract", author_extract.run),
    Stage("author_review", author_review.run),
)
assert tuple(s.name for s in STAGES) == STAGE_NAMES


async def run(
    state: Onboarding,
    ctx: Context,
    *,
    until: "str | None" = None,
    save: "str | Path | None" = None,
) -> Onboarding:
    """Run every stage whose output is missing, in order, stopping after ``until`` (inclusive) or
    when a stage sets ``state.stopped``. The state is saved to ``save`` after each stage."""
    for stage in STAGES:
        if state.stopped:
            break
        if getattr(state, stage.name) is None:
            emit(ReasonEvent(stage=stage.name, text="…"))
            started, t0 = now(), time.monotonic()
            calls0, usd0 = state.spend.calls, state.spend.usd
            out = await stage.run(state, ctx)
            setattr(state, stage.name, out)
            state.log.append(
                StageLog(
                    stage=stage.name,
                    started=started,
                    elapsed_s=round(time.monotonic() - t0, 2),
                    calls=state.spend.calls - calls0,
                    usd=round(state.spend.usd - usd0, 5),
                    note=state.stopped,
                )
            )
            if save is not None:
                state.save(save)
        if stage.name == until:
            break
    return state


__all__ = ["STAGES", "Stage", "run"]
