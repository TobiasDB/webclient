"""PaginationBacking: the manual session's ops (``step`` / ``run`` / ``done``), driven by a
:class:`~webclient.loop.BoundedLoop` -- so a pagination walk is a BoundedLoop like every other
loop in the package, not a hand-rolled one.

The loop maps 1:1: OBSERVE the session, DECIDE the advance (pure -- the next :class:`Reference`
to fetch, or a stop cause), APPLY it (fetch the page, integrate it), and stop when the decision
is terminal. Post-fetch terminals (an empty page, a clamped repeat) are set on the session in
``apply`` and read by the next ``decide``. The advance + stop primitives are shared with the
bound ``doc.paginate`` op (:mod:`..document.paginate`), so the two walk a source identically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator, cast

from ...loop import BoundedLoop, LoopVerdict
from ..document.paginate import (
    _MAX_PAGES_CAP,
    _auto_advance,
    _next_ref,
    _page_fingerprint,
    _past_cutoff,
    _row_count,
)
from ..web_core import Backing
from .models import PaginationVerdict

if TYPE_CHECKING:
    from ..reference import Reference
    from . import Pagination


#: cause -> the LoopVerdict ``reason`` it maps to. A natural end is ``done``; a bound is
#: ``budget``; a clamped repeat is ``stalled`` (no forward progress).
_REASON = {
    "no-next": "done", "empty": "done", "cutoff": "done",
    "rows": "budget", "budget": "budget", "clamp": "stalled",
}


@dataclass(frozen=True)
class _Advance:
    """A round's decision: fetch ``ref`` (the next page), or -- when ``ref`` is ``None`` -- stop
    with ``cause`` (the precise reason the walk ended)."""

    ref: "Reference | None" = None
    cause: str = ""


class PaginationBacking(Backing):
    """The manual pagination ops: ``step`` (fetch one page), ``run`` (walk to the end), and the
    ``done`` predicate. ``step``/``run`` fetch, so they are IO ops (bridged onto the client's
    dispatcher)."""

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
        """Fetch ONE more page (one loop round): compute the next reference and fetch it, or reach
        a stop. A no-op once the walk is done. Lets an agent drive a pager the heuristics can't
        classify page by page, inspecting ``.pages`` / ``.verdict`` between rounds."""
        loop = self._loop(core)
        verdict = await loop.astep(core)
        if verdict is not None:
            self._settle(core, verdict)
        return core

    async def run(self, core: "Pagination") -> "Pagination":
        """Walk the series to completion (the batch drain of :meth:`step`): fetch page after page
        until there is no next page, a page comes back empty, a page repeats (a clamp), a bound is
        hit, or a fetch errors -- then set the :class:`PaginationVerdict`. Composes with ``step``:
        it drives from wherever the walk currently is."""
        loop = self._loop(core)
        verdict: "LoopVerdict | None" = None
        while verdict is None:
            verdict = await loop.astep(core)
        self._settle(core, verdict)
        return core

    async def _astream(self, core: "Pagination") -> "AsyncIterator[Any]":
        """Stream the walk: drive it round by round and yield each page as it is fetched
        (``async for page in pg.stream()``). Pausing (breaking the consumer) leaves the walk state
        intact, so re-entering continues; ``run`` is this stream drained."""
        loop = self._loop(core)
        while True:
            seen = len(core.pages)
            verdict = await loop.astep(core)
            for page in core.pages[seen:]:
                yield page
            if verdict is not None:
                self._settle(core, verdict)
                return

    # -- the loop ---------------------------------------------------------------
    def _loop(self, core: "Pagination") -> "BoundedLoop[Pagination, Pagination, _Advance]":
        """The session's :class:`BoundedLoop`, built once and kept on the session so ``step`` and
        ``run`` share one round counter. ``max_rounds`` is the page budget (page one is round 0)."""
        if core._loop is None:
            cfg = core.config
            client = core._client

            def decide(state: "Pagination") -> _Advance:
                if state._stop:  # a post-fetch terminal set by a previous apply
                    return _Advance(None, state._stop)
                if not state.pages:  # round 0: fetch page one (the source)
                    return _Advance(state._source)
                if cfg.max_rows and state.rows_seen >= cfg.max_rows:
                    return _Advance(None, "rows")
                if cfg.until and cfg.until_before and _past_cutoff(state._current, cfg.until, cfg.until_before):
                    return _Advance(None, "cutoff")
                nxt = _next_ref(
                    state._current, by=state._by, name=state._name, size=cfg.size,
                    start=cfg.start, step=cfg.step, index=len(state.pages),
                    cursor=cfg.cursor, cursor_attr=cfg.cursor_attr,
                )
                return _Advance(nxt) if nxt is not None else _Advance(None, "no-next")

            async def apply(state: "Pagination", adv: _Advance) -> None:
                assert adv.ref is not None  # apply runs only for a fetch decision (done_result gated it)
                page = await client.afetch(adv.ref, optional=True)
                if not (page.ok and page.content):
                    state._stop = "empty"
                    return
                if not state.pages and state._by == "auto":  # resolve the advance off page one
                    state._by, state._name = _auto_advance(page, cfg.name)
                key = _page_fingerprint(page)
                if key in state._seen:
                    state._stop = "clamp"
                    return
                state._seen.add(key)
                state.pages.append(page)
                state.rows_seen += _row_count(page, cfg.records)
                state._current = page

            core._loop = BoundedLoop(
                observe=lambda state, i, err: state,
                decide=decide,
                done_result=lambda adv: adv.cause if adv.ref is None else None,
                apply=apply,
                max_rounds=max(1, min(cfg.max_pages, _MAX_PAGES_CAP)),
                name="paginate",
                bus=getattr(client, "bus", None),
            )
        return cast("BoundedLoop[Pagination, Pagination, _Advance]", core._loop)

    def _settle(self, core: "Pagination", verdict: "LoopVerdict") -> None:
        """Translate the loop's terminal :class:`LoopVerdict` into the session's
        :class:`PaginationVerdict` (the precise stop cause + pages/rows) and close the session."""
        cause = core._stop or (verdict.result if verdict.reason == "done" else verdict.reason)
        reason = _REASON.get(cause, verdict.reason)
        core.verdict = PaginationVerdict(
            done=reason == "done", reason=reason, rounds=len(core.pages),  # type: ignore[arg-type]
            result=cause, error=verdict.error,
            pages=len(core.pages), rows=core.rows_seen, stop=cause,
        )
        core.status = "closed"


__all__ = ["PaginationBacking"]
