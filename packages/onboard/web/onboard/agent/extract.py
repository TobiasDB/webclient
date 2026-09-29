"""Extraction authoring -- the first agent: drive a :class:`BoundedLoop` to find the selectors
that pull rows out of a page.

A :class:`Selection` is a row selector plus a field->sub-selector map; applying it to a Document
yields ``list[dict]``. The agent loops: a **driver** looks at the page and the rows the last
Selection produced and returns a refined Selection, a :class:`Done` (the rows are the answer), or
an :class:`~web.agent.loop.Ask` (hand off to a human). The driver is where the intelligence lives
-- an LLM, a heuristic, or a test stub -- so the loop is exercised without one.

This is deliberately plain: extraction runs on a :class:`~web.parse.Document` (no DSL needed to
author), and a winning Selection maps directly onto a DSL plan
(``ref(url).doc().select_all(row).project(**fields)``) for repeatable execution.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic import BaseModel
from web.parse import Document

from .loop import Ask, BoundedLoop, Done, Verdict


class Selection(BaseModel):
    """A row extraction: the repeating-row selector and each field's sub-selector (relative to a
    row; an empty sub-selector means the row element itself)."""

    row: str
    fields: dict[str, str] = {}


def extract(doc: Document, selection: Selection) -> "list[dict[str, str | None]]":
    """Apply a Selection to a Document -> one dict per row (each field the text of its
    sub-selector, or None when it is absent)."""
    rows: list[dict[str, str | None]] = []
    for el in doc.select_all(selection.row):
        row: dict[str, str | None] = {}
        for name, sub in selection.fields.items():
            found = el.select(sub) if sub else el
            row[name] = found.text if found is not None else None
        rows.append(row)
    return rows


#: what the loop observes each round: the page + the rows the last Selection produced.
_Obs = tuple[Document, "list[dict[str, str | None]]"]
#: a driver looks at the page and the last rows and decides the next move -- sync or async (an
#: LLM driver is async; the loop awaits it).
_Decision = Selection | Done | Ask
Driver = Callable[[Document, list[dict[str, "str | None"]]], "_Decision | Awaitable[_Decision]"]


class Authored(BaseModel):
    """The result: the winning rows, the Selection that produced them, and why the loop stopped."""

    rows: "list[dict[str, str | None]]" = []
    selection: "Selection | None" = None
    verdict: Verdict


class _State:
    def __init__(self, doc: Document) -> None:
        self.doc = doc
        self.rows: "list[dict[str, str | None]]" = []
        self.selection: "Selection | None" = None


class Author:
    """Runs the extraction-authoring loop over a Document, driven by a pluggable ``driver``. Keeps
    the loop so a ``waiting`` verdict can be :meth:`resume`d with a human Selection."""

    def __init__(self, doc: Document, driver: Driver, *, max_rounds: int = 6) -> None:
        self._state = _State(doc)
        self._loop: "BoundedLoop[_State, _Obs, Selection | Done]" = BoundedLoop(
            observe=lambda s: (s.doc, s.rows),
            decide=lambda obs: driver(obs[0], obs[1]),
            apply=self._apply,
            done=lambda d: isinstance(d, Done),
            max_rounds=max_rounds,
        )

    def _apply(self, state: _State, decision: "Selection | Done") -> None:
        if isinstance(decision, Selection):  # Done never reaches apply (done() catches it first)
            state.selection = decision
            state.rows = extract(state.doc, decision)

    async def run(self) -> Authored:
        return self._result(await self._loop.arun(self._state))

    async def resume(self, selection: Selection) -> Authored:
        return self._result(await self._loop.resume(selection))

    def _result(self, verdict: Verdict) -> Authored:
        return Authored(rows=self._state.rows, selection=self._state.selection, verdict=verdict)


__all__ = ["Selection", "Done", "Driver", "Author", "Authored", "extract"]
