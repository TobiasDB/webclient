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


def compile_source(records: str, fields: "dict[str, str]", *, keep: "list[str]" = []) -> str:
    """The query: the records, a presence FILTER per field in ``keep`` (a required field that some
    matched elements lack -- those elements are not records of the dataset), the columns."""
    cols = ", ".join(f"{n}={c}" for n, c in fields.items())
    where = "".join(f".filter({_presence(fields[n])})" for n in keep if n in fields)
    return f"wq.doc.select_all({records!r}){where}.extract({cols})"


def _presence(chain: str) -> str:
    """A column chain as a presence test: its first call made optional, then ``.is_ok()``."""
    head = chain.split(").", 1)[0] + ")"  # up to the first call: wq.doc.select('x') / attr('k')
    if "select(" in head and "optional=True" not in head:
        head = head[:-1] + ", optional=True)"
    return f"{head}.is_ok()"


def lenient(fields: "dict[str, str]") -> "dict[str, str]":
    """Every column's ``select(css)`` made optional -- the PROBE never aborts on one bad field, so
    the fill rate of every field is measured in one run."""
    out: dict[str, str] = {}
    for n, c in fields.items():
        if "select(" in c and "optional=True" not in c:
            head, rest = c.split(")", 1)
            c = head + ", optional=True)" + rest
        out[n] = c
    return out


def fill_rates(rows: "list[JsonValue]", names: "list[str]") -> "dict[str, float]":
    """Per field, the fraction of rows where it read a value."""
    if not rows:
        return {n: 0.0 for n in names}
    return {
        n: sum(1 for r in rows if isinstance(r, dict) and not _empty(r.get(n))) / len(rows)
        for n in names
    }


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
            failure = ""
            rows: list[JsonValue] = []
            try:  # the probe is LENIENT: every field optional, so one bad selector never hides the rest
                probe = reroot(
                    parse_query(compile_source(records, lenient(fields))),
                    plan.url,
                    profile=plan.profile,
                )
                rows = await _probe(probe, resolver, limit=_WINDOW)
            except QueryError as exc:  # selector hygiene refused something: the reason goes back
                failure = str(exc)
            except WebException as exc:
                failure = f"{exc.error.code}: {exc.error.message}"
            except asyncio.TimeoutError:
                failure = "the probe timed out"
            rates = fill_rates(rows, brief.required)
            # a required field read on NO record is a miss (repair); on SOME records it marks the
            # elements that are not records of the dataset (a promo in the list) -> filter them out
            misses = (
                [n for n in brief.required if rates[n] == 0.0] if rows else list(brief.required)
            )
            keep = [n for n in brief.required if 0.0 < rates[n] < 1.0]
            source = compile_source(records, fields, keep=keep)
            out.fields, out.source, out.misses = fields, source, misses
            fill = ", ".join(f"{n} {rates[n]:.0%}" for n in brief.required) if rows else "no rows"
            out.attempts.append(
                f"attempt {attempt + 1}: "
                + (
                    failure
                    or f"{len(rows)} probed row(s); fill: {fill}; misses: {', '.join(misses) or 'none'}"
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
            emit(ReasonEvent(stage="author_extract", text=f"final run: {len(rows)} row(s)"))
    if not out.complete:
        state.stopped = (
            f"author_extract: {out.attempts[-1] if out.attempts else 'nothing extracted'}"
        )
    return out
