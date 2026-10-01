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
from web.dsl import resolve_memo
from web.fetch import Request, WebException, emit
from web.parse import Document, Element
from web.resolve import Resolver, document
from web.resolve import profiles as _rp

from ...compile import Query, QueryError, limited, parse_query, reroot
from ...llm import ReasonEvent
from ..ask import Context, ask_json
from ..hints import attrs_of, closest, leaves, record_structure, typical
from ..state import ExtractQuery, Onboarding

_WINDOW = 10  # records a probe runs over
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


def compile_source(records: str, fields: "dict[str, str]") -> str:
    cols = ", ".join(f"{n}={c}" for n, c in fields.items())
    return f"wq.doc.select_all({records!r}).extract({cols})"


def _empty(v: object) -> bool:
    return (
        v is None
        or (isinstance(v, str) and not v.strip())
        or (isinstance(v, (list, dict)) and not v)
    )


async def _probe(query: Query, resolver: Resolver, *, limit: int) -> "list[JsonValue]":
    q = limited(query, limit) if limit else query
    got = await asyncio.wait_for(q.acollect(resolver=resolver), timeout=60)
    rows = got if isinstance(got, list) else [got]
    out: list[JsonValue] = []
    for r in rows:
        if r is None or isinstance(r, (dict, list, str, int, float)):
            out.append(cast(JsonValue, r))
    return out


def _misses(rows: "list[JsonValue]", names: "list[str]") -> "list[str]":
    out: list[str] = []
    for n in names:
        if all(_empty(r.get(n)) if isinstance(r, dict) else True for r in rows):
            out.append(n)
    return out


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
    out = ExtractQuery(record_selector=records)
    optional = {f.name for f in brief.fields if f.optional}
    note = ""
    specs: dict[str, _Read] = {}
    with resolve_memo():
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
            fields = {
                n: _chain(n, r, json=is_json, optional=n in optional) for n, r in specs.items()
            }
            source = compile_source(records, fields)
            failure = ""
            rows: list[JsonValue] = []
            try:
                query = reroot(parse_query(source), plan.url, profile=plan.profile)
                rows = await _probe(query, resolver, limit=_WINDOW)
            except QueryError as exc:  # selector hygiene refused something: the reason goes back
                failure = str(exc)
            except WebException as exc:
                failure = f"{exc.error.code}: {exc.error.message}"
            except asyncio.TimeoutError:
                failure = "the probe timed out"
            misses = _misses(rows, brief.required) if rows else list(brief.required)
            out.fields, out.source, out.misses = fields, source, misses
            out.attempts.append(
                f"attempt {attempt + 1}: {failure or (str(len(rows)) + ' row(s), misses: ' + (', '.join(misses) or 'none'))}"
            )
            emit(ReasonEvent(stage="author_extract", text=out.attempts[-1]))
            if not failure and rows and not misses:
                break
            note = "\nREPAIR -- fix ONLY what is listed, keep the rest:\n" + _hints(
                doc, records, specs, misses, failure
            )
        if out.source and not out.misses:
            query = reroot(parse_query(out.source), plan.url, profile=plan.profile)
            try:
                rows = await _probe(query, resolver, limit=0)
            except (WebException, asyncio.TimeoutError) as exc:
                out.attempts.append(f"the full run did not finish ({exc})")
                rows = []
            out.row_count, out.sample = len(rows), rows[:5]
            out.blob = query.to_blob()
            out.complete = bool(rows)
    if not out.complete:
        state.stopped = (
            f"author_extract: {out.attempts[-1] if out.attempts else 'nothing extracted'}"
        )
    return out
