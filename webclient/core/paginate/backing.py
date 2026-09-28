"""PaginationBacking: the pager session's ops (``step`` / ``run`` / ``done``) -- the SAME
:class:`~webclient.loop.BoundedLoop` as ``doc.paginate(...)`` (:func:`..document.paginate.pager_loop`),
stepped round by round instead of run out, so an agent can inspect ``.pages`` / ``.verdict`` between
pages. One round = one page (or one concurrent batch on the ``pages`` fast path, or one click / scroll).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, AsyncIterator, cast

from ...kernel.loop import LoopVerdict
from ..document.paginate import pager_loop, take_first
from ..web_core import Backing
from .models import PaginationVerdict

if TYPE_CHECKING:
    from ...kernel.loop import BoundedLoop
    from . import Pagination


class PaginationBacking(Backing):
    """The pager ops: ``step`` (one round), ``run`` (to the end), and the ``done`` predicate. ``step`` /
    ``run`` fetch, so they are IO ops (bridged onto the client's dispatcher)."""

    provides = frozenset({"step", "run"})
    props = frozenset({"done"})
    io = frozenset({"step", "run"})
    gate = "ok"

    def done(self, core: "Pagination") -> bool:
        """Finished: the session is closed or the walk has reached a terminal verdict."""
        return core.status == "closed" or core.verdict is not None

    async def aexit(self, core: "Pagination", *exc: Any) -> None:
        """Close the pagination when its ``with`` block exits."""
        core.status = "closed"

    async def step(self, core: "Pagination") -> "Pagination":
        """ONE round: page one, the next page (or batch), or a click / scroll -- or reach the stop. A
        no-op once the walk is done."""
        if core.verdict is None:
            await self._round(core)
        return core

    async def run(self, core: "Pagination") -> "Pagination":
        """Walk to the end from wherever the walk is (the drain of :meth:`step`), then set the
        :class:`PaginationVerdict`."""
        while core.verdict is None:
            await self._round(core)
        return core

    async def _astream(self, core: "Pagination") -> "AsyncIterator[Any]":
        """Yield each kept page as it lands (``async for page in pg.stream()``). Breaking pauses the
        walk (state intact); ``run`` is this stream drained."""
        while core.verdict is None:
            seen = len(core.pages)
            await self._round(core)
            for page in core.pages[seen:]:
                yield page

    async def _round(self, core: "Pagination") -> None:
        w = core._walk
        if w.first is not None:  # a page one handed in: integrate it as the first round
            await take_first(w, core.config, core._client)
            verdict = LoopVerdict(done=True, reason="done", rounds=1, result=w.cause) if w.cause else None
        else:
            verdict = await self._loop(core).astep(w)
        core.pages = list(w.pages)
        core.rows_seen = w.rows
        if verdict is not None:
            self._settle(core, verdict)

    def _loop(self, core: "Pagination") -> "BoundedLoop[Any, Any, Any]":
        """The session's loop, built once so ``step`` and ``run`` share one round counter."""
        if core._loop is None:
            core._loop = pager_loop(core._walk, core.config, core._client, bus=getattr(core._client, "bus", None))
        return cast("BoundedLoop[Any, Any, Any]", core._loop)

    def _settle(self, core: "Pagination", verdict: LoopVerdict) -> None:
        """The loop's verdict as the session's :class:`PaginationVerdict` (the precise cause + yield)."""
        w = core._walk
        cause = w.cause or (verdict.result if verdict.reason == "done" else verdict.reason)
        # a bound is a budget, a clamped repeat is no progress; the rest are a natural end
        reason = {"budget": "budget", "repeat": "stalled"}.get(cause, verdict.reason)
        core.verdict = PaginationVerdict(
            done=reason == "done", reason=reason, rounds=verdict.rounds, result=cause,  # type: ignore[arg-type]
            error=verdict.error, pages=len(w.pages), fetched=w.fetched, rows=w.rows, stop=cause,
        )
        core.status = "closed"


__all__ = ["PaginationBacking"]
