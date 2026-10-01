"""Stage 8 -- author extract: the FIELD EXTRACTION over the resolved document. The model sees one
typical record's structure and the schema and replies with a selector + read per field (JSON --
no DSL syntax to get wrong); the stage compiles that into the ``wq`` chain through the selector
hygiene, probes a window of records, and on a miss asks ONCE more with precise hints (the closest
selectors, what the record holds, the attributes available). No nested resolves: a record's own
page is a later onboarding of its own."""

from __future__ import annotations

import asyncio
from typing import cast

from pydantic import BaseModel, JsonValue
from web.dsl import Arg, Plan, Query, from_plan, from_source
from web.fetch import Request, WebException, emit
from web.parse import Document, Element
from web.resolve import Resolver, document
from web.resolve import profiles as _rp

from ...compile import QueryError, parse_query, reroot
from ...llm import ReasonEvent
from ..ask import Context, ask_json
from ..hints import attrs_of, closest, leaves, record_structure, typical
from ..state import ExtractQuery, Onboarding

_REPAIRS = 2
_READS = ("text", "href", "src", "datetime", "number")


class _Read(BaseModel):
    css: str = ""  # HTML: the selector relative to the record
    key: str = ""  # JSON: the key / dotted path in the record
    read: str = "text"  # text | href | src | datetime | number | attr:<name>


class _Reply(BaseModel):
    fields: dict[str, _Read] = {}


def _chain(field: str, spec: _Read, *, json: bool, optional: bool) -> str:
    """One column's ``wq.doc`` chain from the model's selector + read."""
    if json:
        chain = f"wq.doc.attr({(spec.key or spec.css)!r})"
    else:
        opt = ", optional=True" if optional else ""
        chain = f"wq.doc.select({spec.css!r}{opt})"
    read = spec.read or "text"
    if read.startswith("attr:"):
        chain += f".attr({read[5:]!r})"
    elif read in ("href", "src"):
        chain += f".attr({read!r})"
    elif read == "datetime":
        chain += '.attr("text").datetime()' if not json else ".datetime()"
    elif read == "number":
        chain += '.attr("text").number()' if not json else ".number()"
    elif not json:
        chain += '.attr("text")'
    return chain


def compile_source(records: str, fields: "dict[str, str]", *, keep: "list[str]" = []) -> str:
    """The query: the records, a presence FILTER per field in ``keep`` (a required field that some
    matched elements lack -- those elements are not records of the dataset), the columns."""
    cols = ", ".join(f"{n}={c}" for n, c in fields.items())
    where = "".join(f".filter({_presence(fields[n])})" for n in keep if n in fields)
    return f"wq.doc.select_all({records!r}){where}.extract({cols})"


def _optional(chain: str) -> str:
    """The column chain with its FIRST ``select`` made optional -- through the plan, never by
    text surgery (a ``:nth-child(2)`` holds a parenthesis of its own)."""
    plan = Plan.from_blob(from_source(chain).to_blob())
    steps = list(plan.steps)
    for i, step in enumerate(steps):
        nxt = steps[i + 1] if i + 1 < len(steps) else None
        if step.kind == "get" and step.name == "select" and nxt is not None and nxt.kind == "call":
            if "optional" not in nxt.kwargs:
                steps[i + 1] = nxt.model_copy(
                    update={"kwargs": {**nxt.kwargs, "optional": Arg(value=True)}}
                )
            break
    return from_plan(plan.model_copy(update={"steps": steps})).to_source()


def _presence(chain: str) -> str:
    """A column chain as a presence test of its VALUE: the chain made optional, ``~….is_empty()``
    (an element that is there but reads nothing does not count -- the same rule as the fill rate).
    """
    return f"~{_optional(chain)}.is_empty()"


def _hints(
    doc: Document, records: str, fields: "dict[str, _Read]", misses: "list[str]", failure: str
) -> str:
    """Precise per-field hints for the repair turn."""
    lines: list[str] = []
    if failure:
        lines.append(f"The query FAILED: {failure}")
    scope: "Document | Element | None" = None
    if doc.kind != "json":
        els = doc.select_all(records)
        scope = els[typical(doc, records)] if els else None
    for name in misses:
        spec = fields.get(name)
        css = spec.css if spec is not None else ""
        line = f"- {name}: {css!r} read nothing on every probed record."
        if scope is not None and css:
            near = closest(scope, css)
            if near:
                line += " Closest selectors in the record: " + ", ".join(near) + "."
            have = attrs_of(scope, css)
            if have:
                line += " Its attributes: " + ", ".join(have) + "."
        lines.append(line)
    if scope is not None:
        what = leaves(scope)
        if what:
            lines.append("What the record holds (selector (text)): " + "; ".join(what))
    return "\n".join(lines)


async def run(state: Onboarding, ctx: Context) -> ExtractQuery:
    assert state.author_resolve is not None and state.expand is not None
    plan, src, brief = state.author_resolve, state.expand, state.brief
    prof = _rp.get(plan.profile) or _rp.BASIC
    resolver = Resolver(profile=prof, pool=ctx.resolver.pool)
    doc = document(await resolver.snapshot(Request(url=plan.url)))
    is_json = doc.kind == "json"
    records = (
        (src.api.records_path if (plan.via_api and src.api is not None) else "")
        if is_json
        else src.record_selector
    )
    if not is_json and not records:
        regions = doc.records(top_k=1)
        records = regions[0].item_selector if regions else ""
    if not is_json and not records:
        state.stopped = "author_extract: no repeating record region on the document"
        return ExtractQuery(attempts=["no record selector"])
    structure = record_structure(doc, records)
    emit(
        ReasonEvent(
            stage="author_extract",
            subject=plan.url,
            text=f"{doc.kind} document; records at {records!r}; the typical record "
            f"({len(structure)} chars):\n{structure}",
        )
    )
    out = ExtractQuery(record_selector=records)
    optional = {f.name for f in brief.fields if f.optional}
    note = ""
    specs: dict[str, _Read] = {}
    schema = brief.as_schema()
    for attempt in range(1 + _REPAIRS):
        reply = await ask_json(
            ctx,
            state,
            "author_extract",
            _Reply,
            goal=brief.goal,
            schema=brief.schema_lines(),
            kind=(
                "JSON record (keys)"
                if is_json
                else "HTML record (CSS selectors, relative to the record)"
            ),
            records=records,
            count=str(src.records),
            structure=structure,
            hint=brief.hints.get("author_extract", ""),
            note=note,
        )
        specs = {n: r for n, r in reply.fields.items() if n in brief.names}
        if not specs:
            note = "\nREPAIR: your reply named none of the schema's fields. Use the field names exactly."
            out.attempts.append(f"attempt {attempt + 1}: no schema field in the reply")
            continue
        fields = {n: _chain(n, r, json=is_json, optional=n in optional) for n, r in specs.items()}
        failure = ""
        rates: dict[str, float] = {n: 0.0 for n in brief.required}
        rows = 0
        try:  # the probe is a LENIENT run: every field optional, so one bad selector never hides
            # the rest, and the report's fill rates say what each field read
            probe = Query.of(
                reroot(parse_query(compile_source(records, fields)), plan.url, profile=plan.profile)
            ).with_schema(schema)
            result = await probe.run(resolver, lenient=True)
            rows = result.report.rows
            rates = {n: result.report.fill.get(n, 0.0) for n in brief.required}
            if result.report.failures:
                failure = "; ".join(result.report.failures)
        except QueryError as exc:  # selector hygiene refused something: the reason goes back
            failure = str(exc)
        except Exception as exc:  # noqa: BLE001 -- a model-written selector the engine rejects
            failure = f"the query could not run ({type(exc).__name__}: {exc})"
        # a required field read on NO record is a miss (repair); on SOME records it marks the
        # elements that are not records of the dataset (a promo in the list) -> filter them out
        misses = [n for n in brief.required if rates[n] == 0.0] if rows else list(brief.required)
        keep = [n for n in brief.required if 0.0 < rates[n] < 1.0]
        source = compile_source(records, fields, keep=keep)
        out.fields, out.source, out.misses = fields, source, misses
        fill = ", ".join(f"{n} {rates[n]:.0%}" for n in brief.required) if rows else "no rows"
        out.attempts.append(
            f"attempt {attempt + 1}: "
            + (
                failure
                or f"{rows} probed row(s); fill: {fill}; misses: {', '.join(misses) or 'none'}"
                + (f"; filtering records lacking {', '.join(keep)}" if keep else "")
            )
        )
        emit(ReasonEvent(stage="author_extract", text=out.attempts[-1]))
        emit(ReasonEvent(stage="author_extract", text=f"attempt {attempt + 1} query: {source}"))
        if not failure and rows and not misses:
            break
        hints = _hints(doc, records, specs, misses, failure)
        emit(ReasonEvent(stage="author_extract", text=f"repair hints:\n{hints}"))
        note = "\nREPAIR -- fix ONLY what is listed, keep the rest:\n" + hints
    if out.source and not out.misses:  # the final run is LOUD (the default): the authored query
        query = Query.of(
            reroot(parse_query(out.source), plan.url, profile=plan.profile)
        ).with_schema(schema)
        final = await query.run(resolver)
        if final.report.failures:
            out.attempts.append("the full run failed: " + "; ".join(final.report.failures))
        out.row_count = final.report.rows
        out.sample = list(final.rows[:5])
        out.blob = query.to_blob()
        out.report = final.report.summary()
        out.complete = bool(final.rows) and not final.report.failures
        emit(ReasonEvent(stage="author_extract", text=f"final run: {final.report.summary()}"))
    if not out.complete:
        state.stopped = (
            f"author_extract: {out.attempts[-1] if out.attempts else 'nothing extracted'}"
        )
    return out
