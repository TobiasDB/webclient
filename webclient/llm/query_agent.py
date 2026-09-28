"""A type-safe query-building loop -- the query twin of the interaction loop.

Where the interaction loop drives a LIVE page (it must iterate, the DOM changes per action),
the query loop authors an EXTRACTION over a STATIC resolved page. A page doesn't change, so the
target is ONE-SHOT: pick the record, pick its fields, done. But a genuinely COMPLEX extraction
(more sections, tricky fields) is built more reliably over a bounded few rounds -- so this runs
on the same :class:`~webclient.loop.BoundedLoop`, growing the query round by round until the
model declares it complete (or a budget is hit). Validation failure (0 rows) feeds back as an
``error`` the model corrects next round.

The model reasons purely in INDEXES: it sees the record options and the field options of the
chosen record (each already carrying a durable, per-row selector -- see
:mod:`..core.document.element_index`) and returns ``(record#, {name: field#})``. The loop
resolves the indexes to selectors and assembles a normal
``wq.doc.select_all(record).extract(**fields).project()`` Expr -- so the model never authors a
selector, yet the result is an ordinary serialisable query Plan that ``.collect()``s the rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from ..core.document.element_index import field_options, record_options
from ..core.document.html import tree
from ..core.document.models import IndexedElement
from ..interface import wq
from ..kernel.loop import BoundedLoop

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..interface import Document


# -- what the policy sees / returns -----------------------------------------
class QueryObservation(BaseModel):
    """A snapshot handed to the query policy each round: the loop budget, the numbered ``records``
    (repeated-region options for ``select_all``) and ``fields`` (the chosen/top record's
    extractable leaves), the ``query`` authored so far (its readable form), a ``sample`` of the
    rows it currently extracts, and the last ``error`` (e.g. 0 rows) to correct."""

    step: int
    max_steps: int
    records: "list[IndexedElement]" = []
    fields: "list[IndexedElement]" = []
    query: str = ""
    sample: list[dict[str, Any]] = []
    error: str = ""


class QueryDecision(BaseModel):
    """The increment the policy returns: ``record`` selects a record option by index (set it once,
    ``None`` to keep the current one); ``fields`` adds extract columns as ``{name: field_index}``;
    ``done`` declares the query complete (the loop ends -- typically after the sample looks right)."""

    record: int | None = None
    fields: dict[str, int] = {}
    done: bool = False


class QueryRun(BaseModel):
    """The outcome of a :func:`build_query` loop: the authored query (``blob`` to rebuild it,
    ``describe`` to read it), the ``row_count`` + ``sample`` it extracts, and the loop verdict
    (``done``/``reason``/``rounds``). ``error`` carries a validation message (e.g. 0 rows)."""

    done: bool
    reason: str
    rounds: int
    blob: str = ""
    describe: str = ""
    row_count: int = 0
    sample: list[dict[str, Any]] = []
    error: str = ""


#: the query "brain": given a :class:`QueryObservation`, return the next :class:`QueryDecision`.
QueryPolicy = "Callable[[QueryObservation], QueryDecision]"


@dataclass
class _Increment:
    """A decision with its indexes already RESOLVED to selectors (record item selector + per-row
    field selectors), so the loop's ``apply`` can merge it without needing the observation."""

    record_sel: str | None
    cols: dict[str, str]
    done: bool


@dataclass
class _QueryState:
    """The query-in-progress the loop mutates: the chosen record selector, the extract columns
    (name -> per-row field selector), and the last run's sample / row count / validation error."""

    doc: Any
    record_sel: str | None = None
    cols: dict[str, str] = field(default_factory=dict)
    sample: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    error: str = ""

    def expr(self) -> Any:
        """The query-so-far as a ``wq.doc.select_all(record).extract(**fields).project()`` Expr,
        or ``None`` before a record is chosen. With no fields yet, extract the record's own text
        so the query is runnable (a preview)."""
        if not self.record_sel:
            return None
        base = wq.doc.select_all(self.record_sel)
        if self.cols:
            cols = {name: wq.doc.select(sel).attr("text") for name, sel in self.cols.items()}
            return base.extract(**cols).project()
        return base.extract(_=wq.doc.attr("text")).project()

    @property
    def key(self) -> Any:
        """A hashable signature of the query-so-far -- equal across rounds means no progress."""
        return (self.record_sel, tuple(sorted(self.cols.items())))


def _resolve(decision: QueryDecision, obs: QueryObservation) -> _Increment:
    """Resolve a decision's indexes against the observation: the ``record`` index -> its item
    selector, each ``fields`` index -> its per-row field selector. An index with no matching
    option is dropped (the model can re-pick next round)."""
    record_sel = None
    if decision.record is not None:
        r = next((e for e in obs.records if e.index == decision.record), None)
        record_sel = r.selector if r is not None else None
    cols: dict[str, str] = {}
    for name, idx in decision.fields.items():
        f = next((e for e in obs.fields if e.index == idx), None)
        if f is not None:
            cols[name] = f.selector
    return _Increment(record_sel=record_sel, cols=cols, done=decision.done)


def _observe(state: _QueryState, step: int, max_steps: int, error: str) -> QueryObservation:
    """Snapshot the query-building state: the record options, the field options of the chosen (or
    top) record, the query-so-far, its current sample, and any validation error to correct."""
    root = tree(state.doc)
    records = record_options(root)
    active = state.record_sel or (records[0].selector if records else None)
    fields = field_options(root, active) if active else []
    return QueryObservation(
        step=step, max_steps=max_steps, records=records, fields=fields,
        query=state.expr().describe() if state.expr() is not None else "",
        sample=state.sample, error=state.error or error,
    )


def _apply(state: _QueryState, inc: _Increment) -> None:
    """Merge one increment into the query and re-run it for a fresh sample. Choosing a new record
    resets the columns (they were scoped to the old one); a 0-row result records a validation
    error the model sees next round."""
    if inc.record_sel and inc.record_sel != state.record_sel:
        state.record_sel = inc.record_sel
        state.cols = {}
    state.cols.update(inc.cols)
    expr = state.expr()
    if expr is None or not state.cols:
        return  # a record with no fields yet -> nothing to run
    try:
        rows = expr.collect(state.doc)
    except Exception as exc:  # noqa: BLE001 - a bad selector -> feed the failure back, don't crash
        state.sample = []
        state.row_count = 0
        state.error = f"the query failed to run: {exc}"
        return
    data = [r for r in rows if isinstance(r, dict)]
    state.sample = data[:5]
    state.row_count = len(data)
    state.error = "" if state.row_count else "the query matched 0 rows -- fix the record or fields"


def build_query(
    doc: "Document", policy: "Callable[[QueryObservation], Any]", *, max_rounds: "int | None" = None
) -> QueryRun:
    """Author an extraction query for ``doc`` with ``policy`` -- the query twin of ``drive``. Each
    round the policy sees the record + field options (by index) and the current sample, and returns
    a :class:`QueryDecision` (pick the record, add fields, or declare done); the loop resolves the
    indexes to durable selectors and grows a ``select_all(...).extract(...).project()`` Expr,
    re-running it for a sample. Bounded by ``max_rounds``; a 0-row query feeds back as an error to
    correct. Returns the authored query (blob/describe), its sample, and the verdict."""
    from ..settings import current

    if max_rounds is None:
        max_rounds = current().loops.query_max_rounds
    state = _QueryState(doc=doc)

    def decide(obs: QueryObservation) -> _Increment:
        # resolve the picked indexes to selectors and APPLY the increment (merge + re-run) here,
        # so a decision that adds fields AND declares ``done`` applies the fields before the loop
        # terminates on ``done_result`` (which runs after decide, before the base's no-op apply).
        inc = _resolve(policy(obs), obs)
        _apply(state, inc)
        return inc

    loop: "BoundedLoop[_QueryState, QueryObservation, _Increment]" = BoundedLoop(
        observe=lambda s, i, err: _observe(s, i, max_rounds, err),
        decide=decide,
        done_result=lambda inc: "done" if inc.done else None,
        apply=lambda s, inc: None,  # the increment is already applied in ``decide``
        progress=lambda s: s.key,
        max_rounds=max_rounds,
        max_stalls=2,
        name="query loop",
        bus=getattr(getattr(doc, "_client", None), "bus", None),
    )
    verdict = loop.run(state)
    expr = state.expr()
    return QueryRun(
        done=verdict.done, reason=verdict.reason, rounds=verdict.rounds,
        blob=expr.to_blob() if expr is not None else "",
        describe=expr.describe() if expr is not None else "",
        row_count=state.row_count, sample=state.sample,
        error=state.error or verdict.error,
    )


__all__ = [
    "QueryObservation", "QueryDecision", "QueryRun", "QueryPolicy", "build_query",
]
