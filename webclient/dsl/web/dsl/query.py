"""``Query`` -- the executable, portable form of a recorded chain, and ``Run`` -- what running it
yields. ONE interface whether or not the plan carries a schema:

    q = Query.from_source("wq.reference(url).resolve().select_all('li').extract(t=...)")
    run = await q.run(resolver)              # -> Run(rows, documents, report)
    run = await q.run(resolver, sink=my_db)  # rows / documents streamed to a sink as they come
    async for ev in q.stream(resolver): ...  # the same, as events

Loud by default: a required selector that misses raises (as the chain would). ``lenient=True``
makes every ``select`` optional for the run and records what each row lacked in its ``_issues``
column (and the report), so one bad field never hides the rest -- what a production run and an
author's probe want. Documents: a field the schema types as a document holds a file URL the run
fetches; a page the chain fanned out into and identified is a document too (from the run's memo).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import cast

from pydantic import JsonValue
from web.fetch import Event, EventBus, FetchEvent, WebException, current, using
from web.resolve import ResolveEvent, Resolver

from .expr import Expr, from_plan
from .identity import URL_COLUMN
from .plan import Arg, Plan, Step
from .report import ISSUES_COLUMN, Fetch, Issue, Report, assess
from .run import resolve_memo
from .schema import Schema
from .sink import Attachment, Dataset, Sink, content_type_of
from .surface import from_source


@dataclass
class RunEvent:
    """One thing a streamed run produced: a ``row``, a ``document``, or the final ``report``."""

    kind: str  # row | document | report
    row: "dict[str, JsonValue] | None" = None
    document: "Attachment | None" = None
    report: "Report | None" = None


@dataclass
class Run:
    rows: "list[dict[str, JsonValue]]" = field(default_factory=list)
    documents: "list[Attachment]" = field(default_factory=list)
    report: Report = field(default_factory=Report)


class Query:
    """A plan with its (optional) schema, ready to run, serialise or describe."""

    def __init__(self, plan: Plan) -> None:
        self._plan = plan.validate_names()

    # -- building --

    @classmethod
    def of(cls, expr: Expr) -> "Query":
        return cls(Plan.from_blob(expr.to_blob()))

    @classmethod
    def from_blob(cls, blob: str) -> "Query":
        return cls(Plan.from_blob(blob))

    @classmethod
    def from_plan(cls, plan: "Plan | dict[str, object] | str") -> "Query":
        return cls.of(from_plan(plan))

    @classmethod
    def from_source(cls, source: str) -> "Query":
        return cls.of(from_source(source))

    def with_schema(self, schema: "Schema | None") -> "Query":
        return Query(self._plan.model_copy(update={"schema_": schema}))

    # -- reading --

    @property
    def plan(self) -> Plan:
        return self._plan

    @property
    def schema(self) -> "Schema | None":
        return self._plan.schema_

    @property
    def expr(self) -> Expr:
        return Expr(self._plan)

    def to_blob(self) -> str:
        return self._plan.to_blob()

    def describe(self) -> str:
        return self._plan.describe()

    def to_source(self) -> str:
        return self._plan.to_source()

    def __repr__(self) -> str:
        return f"Query({self._plan.describe()})"

    # -- running --

    async def run(
        self,
        resolver: "Resolver | None" = None,
        *,
        sink: "Sink | None" = None,
        lenient: bool = False,
    ) -> Run:
        """Run and collect: rows, documents and the report (also streamed to ``sink`` as they
        come). A resolver built here is closed on the way out."""
        out = Run()
        async for ev in self.stream(resolver, sink=sink, lenient=lenient):
            if ev.kind == "row" and ev.row is not None:
                out.rows.append(ev.row)
            elif ev.kind == "document" and ev.document is not None:
                out.documents.append(ev.document)
            elif ev.report is not None:
                out.report = ev.report
        return out

    async def stream(
        self,
        resolver: "Resolver | None" = None,
        *,
        sink: "Sink | None" = None,
        lenient: bool = False,
    ) -> AsyncIterator[RunEvent]:
        """The run as events: each row (recorded to ``sink``), each document (fetched; sent to
        ``sink``), then the report. Fetches are recorded from the bus; the caller's bus still
        receives everything."""
        own = resolver is None
        rs = resolver if resolver is not None else Resolver()
        plan = _lenient_plan(self._plan) if lenient else self._plan
        schema = self._plan.schema_
        types = schema.types() if schema is not None else {}
        docs = set(schema.documents) if schema is not None else set()
        required = schema.required if schema is not None else []
        fetches: list[Fetch] = []
        failures: list[str] = []
        bus = _forwarding_bus(fetches)
        t0 = time.monotonic()
        try:
            with using(bus), resolve_memo():
                try:
                    got = await Expr(plan).acollect(resolver=rs)
                except WebException as exc:
                    failures.append(f"{exc.error.code}: {exc.error.message}")
                    got = []
                listed = got if isinstance(got, list) else [got]
                rows: list[JsonValue] = []
                issues: list[Issue] = []
                for i, item in enumerate(listed):
                    if isinstance(item, dict):
                        row = cast("dict[str, JsonValue]", item)
                        if lenient:
                            lacking = [n for n in required if _empty(row.get(n))]
                            if lacking:
                                row[ISSUES_COLUMN] = [f"missing {n}" for n in lacking]
                                issues.extend(
                                    Issue(row=i, field=n, message="missing") for n in lacking
                                )
                        rows.append(row)
                        if sink is not None:
                            await sink.record(row, schema=types or _types_of(row))
                        yield RunEvent(kind="row", row=row)
                        async for att in _documents_of(row, docs, rs):
                            if sink is not None:
                                await sink.blob(
                                    att.content,
                                    url=att.url,
                                    content_type=att.content_type,
                                    metadata=att.metadata,
                                )
                            yield RunEvent(kind="document", document=att)
                    elif isinstance(item, str) and item.startswith(("http://", "https://")):
                        try:  # a bare-URL listing: every result is a document
                            doc = await rs.resolve(item)
                        except WebException as exc:
                            failures.append(f"{item}: {exc.error.code}")
                            continue
                        att = Attachment(
                            url=item, content=doc.content, content_type=content_type_of(item)
                        )
                        if sink is not None:
                            await sink.blob(
                                att.content, url=att.url, content_type=att.content_type, metadata={}
                            )
                        yield RunEvent(kind="document", document=att)
                    else:
                        rows.append(cast(JsonValue, item))
                        yield RunEvent(kind="row", row={"value": cast(JsonValue, item)})
            report = assess(rows, schema)
            report.issues, report.fetches, report.failures = issues, fetches, failures
            report.elapsed_s = round(time.monotonic() - t0, 2)
            yield RunEvent(kind="report", report=report)
        finally:
            if own:
                await rs.aclose()


def _empty(v: object) -> bool:
    return (
        v is None
        or (isinstance(v, str) and not v.strip())
        or (isinstance(v, (list, dict)) and not v)
    )


def _types_of(row: "dict[str, JsonValue]") -> "dict[str, str]":
    return {k: ("identity" if k == "_identity" else "string") for k in row}


async def _documents_of(
    row: "dict[str, JsonValue]", docs: "set[str]", rs: Resolver
) -> AsyncIterator[Attachment]:
    """The documents a row points at: each document-typed field's URL, and each identified page
    the chain fanned out into (nested rows carrying ``_url``), with the row as metadata."""
    scalars: dict[str, JsonValue] = {
        k: v for k, v in row.items() if not isinstance(v, (dict, list))
    }
    for page in _pages(row):
        url = str(page[URL_COLUMN])
        try:
            doc = await rs.resolve(url)
        except WebException:
            continue
        yield Attachment(
            url=url,
            content=doc.content,
            content_type=content_type_of(url),
            metadata={"row": scalars, "fields": page},
        )
    for name in docs:
        cell = row.get(name)
        if isinstance(cell, str) and cell.startswith(("http://", "https://")):
            try:
                doc = await rs.resolve(cell)
            except WebException:
                continue
            yield Attachment(
                url=cell,
                content=doc.content,
                content_type=content_type_of(cell),
                metadata={"row": scalars, "field": name},
            )


def _pages(value: JsonValue) -> "list[dict[str, JsonValue]]":
    found: list[dict[str, JsonValue]] = []
    if isinstance(value, dict):
        if isinstance(value.get(URL_COLUMN), str):
            found.append(value)
        for v in value.values():
            found.extend(_pages(v))
    elif isinstance(value, list):
        for v in value:
            found.extend(_pages(v))
    return found


def _forwarding_bus(fetches: "list[Fetch]") -> EventBus:
    """A bus that records the run's fetches / escalations and forwards EVERY event to the caller's
    ambient bus (so a CLI's progress keeps printing)."""
    parent = current()
    bus = EventBus()
    tiers: dict[str, int] = {}

    def on(event: Event) -> None:
        if isinstance(event, ResolveEvent) and event.phase in ("escalate", "sticky"):
            tier = event.detail.get("tier")
            tiers[event.url] = int(tier) if isinstance(tier, (int, float)) else 0
        elif isinstance(event, FetchEvent):
            fetches.append(
                Fetch(
                    url=event.url,
                    status=event.status,
                    elapsed=round(event.elapsed, 3),
                    tier=tiers.get(event.url, 0),
                )
            )
        if parent is not None:
            parent.publish(event)

    bus.subscribe("", on)
    return bus


#: the verbs a lenient run makes optional: every navigation that may miss on a record.
_OPTIONAL_VERBS = frozenset({"select", "next", "prev"})


def _lenient_plan(plan: Plan) -> Plan:
    """Every ``select`` / ``next`` / ``prev`` call in the plan (sub-plans included) made optional."""

    def walk(p: Plan) -> Plan:
        steps = list(p.steps)
        for i, step in enumerate(steps):
            nxt = steps[i + 1] if i + 1 < len(steps) else None
            if (
                step.kind == "get"
                and step.name == "select"
                and nxt is not None
                and nxt.kind == "call"
            ):
                if "optional" not in nxt.kwargs:
                    steps[i + 1] = nxt.model_copy(
                        update={"kwargs": {**nxt.kwargs, "optional": Arg(value=True)}}
                    )
            if step.kind == "call":
                steps[i] = step.model_copy(
                    update={
                        "args": [
                            Arg(plan=walk(a.plan)) if a.plan is not None else a for a in step.args
                        ],
                        "kwargs": {
                            k: (Arg(plan=walk(a.plan)) if a.plan is not None else a)
                            for k, a in step.kwargs.items()
                        },
                    }
                )
        return p.model_copy(update={"steps": steps})

    return walk(plan)


__all__ = ["Query", "Run", "RunEvent"]
