"""Stage 8 -- author extract: the extraction over the resolved document, written by the model from
the DOCUMENT and the brief: the record selector (the repeating element) and, per field, a selector
+ read -- as JSON, no DSL syntax to get wrong. The stage compiles that into the ``wq`` chain through
the selector hygiene, probes it LENIENTLY (every field optional: one bad selector never hides the
rest; the report's fill rates say what each field read), and on a miss asks again with precise
hints: how many elements the record selector matched, a typical record's structure, the closest
selectors, what the record holds. No nested resolves: a record's own page is a later onboarding."""

from __future__ import annotations

import datetime as _dt

from pydantic import BaseModel
from web.dsl import Arg, Plan, Query, from_plan, from_source
from web.fetch import Request, emit
from web.parse import Document, Element
from web.parse.selectors import normalise, problems
from web.resolve import Resolver, document
from web.resolve import profiles as _rp

from ...compile import QueryError, parse_query, reroot
from ...llm import ReasonEvent
from ..apis import records_path
from ..ask import Context, ask_json
from ..brief import QueryGuide
from ..hints import attrs_of, closest, leaves, record_structure, typical
from ..state import ExtractQuery, Onboarding
from .review_candidate import skeleton
from .review_location import describe

_REPAIRS = 2


class _Read(BaseModel):
    css: str = ""  # HTML: the selector relative to the record
    key: str = ""  # JSON: the key / dotted path in the record
    read: str = "text"  # text | href | src | datetime | number | attr:<name>


class _Reply(BaseModel):
    records: str = ""  # the repeating element (css) / the record array (json path)
    where: str = ""  # optional: a wq.doc predicate keeping only THIS query's records
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


def compile_source(
    records: str, fields: "dict[str, str]", *, keep: "list[str]" = [], where: str = ""
) -> str:
    """The query: the records, the author's ``where`` predicate (this query's records only: the
    upcoming events by date), a presence FILTER per field in ``keep`` (a required field that some
    matched elements lack -- those elements are not records of the dataset), the columns."""
    cols = ", ".join(f"{n}={c}" for n, c in fields.items())
    pred = f".filter({where})" if where else ""
    presence = "".join(f".filter({_presence(fields[n])})" for n in keep if n in fields)
    return f"wq.doc.select_all({records!r}){pred}{presence}.extract({cols})"


def _check_where(where: str) -> str:
    """``""`` when the predicate is a parseable ``wq.doc`` chain, else the reason."""
    if not where:
        return ""
    try:
        from_source(where)
    except Exception as exc:  # noqa: BLE001 -- the DSL's own parse error is the reason
        return f"'where' is not a valid wq.doc predicate ({exc})"
    return (
        ""
        if where.lstrip().startswith(("wq.doc", "~wq.doc", "(wq.doc"))
        else "'where' must start with wq.doc"
    )


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


def _empty(v: object) -> bool:
    return (
        v is None
        or (isinstance(v, str) and not v.strip())
        or (isinstance(v, (list, dict)) and not v)
    )


def matches(doc: Document, css: str) -> "tuple[int, str]":
    """``(how many elements css matches, "")`` -- or ``(0, why)`` when the selector is not valid
    CSS (a model writes one now and then; the engine's parse error is the reason it hears)."""
    try:
        return len(doc.select_all(css)), ""
    except Exception as exc:  # noqa: BLE001 -- cssselect / lxml raise their own types
        return 0, f"{css!r} is not a valid selector ({type(exc).__name__}: {exc})"


def _hints(
    doc: Document,
    records: str,
    matched: int,
    fields: "dict[str, _Read]",
    misses: "list[str]",
    failure: str,
) -> str:
    """Precise hints for the repair turn: the record selector's match count and a typical record,
    then per missed field the closest selectors, its attributes, what the record holds."""
    lines: list[str] = []
    if failure:
        lines.append(f"The query FAILED: {failure}")
    scope: "Document | Element | None" = None
    if doc.kind != "json":
        if matched == 0:
            lines.append(
                f"- records: {records!r} matched NO element. Pick the element that repeats once per "
                "record from the page structure (a tag plus a semantic class or attribute)."
            )
        elif not matches(doc, records)[1]:
            lines.append(
                f"- records: {records!r} matched {matched} element(s); a typical one:\n{record_structure(doc, records)}"
            )
            els = doc.select_all(records)
            scope = els[typical(doc, records)] if els else None
    for name in misses:
        spec = fields.get(name)
        css = spec.css or spec.key if spec is not None else ""
        line = f"- {name}: {css!r} read nothing on every record."
        if scope is not None and css:
            try:
                near, have = closest(scope, css), attrs_of(scope, css)
            except Exception as exc:  # noqa: BLE001 -- the field's selector is not valid CSS
                line += f" (not a valid selector: {exc})"
            else:
                if near:
                    line += " Closest selectors in the record: " + ", ".join(near) + "."
                if have:
                    line += " Its attributes: " + ", ".join(have) + "."
        lines.append(line)
    if scope is not None:
        what = leaves(scope)
        if what:
            lines.append("What the record holds (selector (text)): " + "; ".join(what))
    return "\n".join(lines)


async def run(state: Onboarding, ctx: Context) -> "list[ExtractQuery]":
    """One query per authoring guide of the brief (``queries:``; else one), over the same fetched
    document. A guide that stops the run stops it with its name."""
    assert state.author_resolve is not None and state.expand is not None
    plan = state.author_resolve
    prof = _rp.get(plan.profile) or _rp.BASIC
    resolver = Resolver(profile=prof, pool=ctx.resolver.pool)
    doc = document(await resolver.snapshot(Request(url=plan.url)))
    outline = skeleton(doc)  # the WHOLE structure: the author chooses from all of it
    emit(
        ReasonEvent(
            stage="author_extract",
            subject=plan.url,
            text=f"{doc.kind} document ({len(doc.content)} bytes); the structure shown "
            f"({len(outline)} chars):\n{outline}",
        )
    )
    out: list[ExtractQuery] = []
    for guide in state.brief.guides():
        if guide.name:
            emit(
                ReasonEvent(
                    stage="author_extract", text=f"query {guide.name!r}: {guide.hint[:120]}"
                )
            )
        one = await _one(state, ctx, guide, doc, outline, resolver)
        out.append(one)
        if not one.complete:
            state.stopped = f"author_extract{' (' + guide.name + ')' if guide.name else ''}: " + (
                one.attempts[-1] if one.attempts else "nothing extracted"
            )
            break
    return out


async def _one(
    state: Onboarding,
    ctx: Context,
    guide: QueryGuide,
    doc: Document,
    outline: str,
    resolver: Resolver,
) -> ExtractQuery:
    assert state.author_resolve is not None
    plan, brief = state.author_resolve, state.brief
    fields_in = brief.guide_fields(guide)
    names = [f.name for f in fields_in]
    required = [f.name for f in fields_in if not f.optional]
    is_json = doc.kind == "json"
    out = ExtractQuery(name=guide.name)
    optional = {f.name for f in fields_in if f.optional}
    note = (
        f"\nREPAIR -- a reviewer rejected the previous extraction: {state.review_note}\n"
        "Fix what it names; keep the rest."
        if state.review_note
        else ""
    )
    schema = brief.as_schema(guide)
    guidance = (brief.hints.get("author_extract", "") + "\n" + guide.hint).strip()
    if guide.name:
        guidance = f"THIS QUERY: {guide.name} -- {guide.hint}".strip()
    for attempt in range(1 + _REPAIRS):
        reply = await ask_json(
            ctx,
            state,
            "author_extract",
            _Reply,
            goal=brief.goal,
            schema="\n".join(
                f"- {f.name} ({f.type}): {f.description}" + (" [optional]" if f.optional else "")
                for f in fields_in
            ),
            kind=(
                "a JSON document: 'records' is the dotted path to the record array (\"\" when "
                "the document IS the array), a field reads a key of each record"
                if is_json
                else "an HTML document: 'records' is the CSS selector of the element that repeats "
                "once per record, a field reads an element RELATIVE to that record"
            ),
            skeleton=outline,
            hint=guidance,
            source=describe(state.expand) if state.expand is not None else "",
            today=_dt.date.today().isoformat(),
            note=note,
        )
        records = reply.records.strip() if not is_json else reply.records.strip()
        if is_json and not records:
            records = records_path(doc.json())
        specs = {n: r for n, r in reply.fields.items() if n in names}
        where = reply.where.strip()
        failure = _check_where(where)
        if not is_json and not failure:
            records = normalise(records)
            wrong = (
                problems(records, "records") if records else ["no 'records' selector in the reply"]
            )
            if wrong:
                failure = " ".join(wrong)
        if not specs and not failure:
            failure = "the reply named none of the schema's fields (use the field names exactly)"
        fields = {n: _chain(n, r, json=is_json, optional=n in optional) for n, r in specs.items()}
        matched = 0
        if records and not is_json and not failure:
            matched, bad = matches(doc, records)
            if bad:
                failure = bad
        rates: dict[str, float] = {n: 0.0 for n in required}
        rows = 0
        if not failure:
            try:  # a LENIENT probe: every field optional; the report's fill rates say what read
                probe = Query.of(
                    reroot(
                        parse_query(compile_source(records, fields, where=where)),
                        plan.url,
                        profile=plan.profile,
                    )
                ).with_schema(schema)
                result = await probe.run(resolver, lenient=True)
                rows = result.report.rows
                rates = {n: result.report.fill.get(n, 0.0) for n in required}
                if result.report.failures:
                    failure = "; ".join(result.report.failures)
            except QueryError as exc:  # selector hygiene refused something: the reason goes back
                failure = str(exc)
            except Exception as exc:  # noqa: BLE001 -- a model-written selector the engine rejects
                failure = f"the query could not run ({type(exc).__name__}: {exc})"
        # a required field read on NO record is a miss (repair); on SOME records it marks the
        # elements that are not records of the dataset (a promo in the list) -> filter them out
        misses = [n for n in required if rates[n] == 0.0] if rows else list(required)
        keep = [n for n in required if 0.0 < rates[n] < 1.0]
        source = (
            compile_source(records, fields, keep=keep, where=where) if records and fields else ""
        )
        out.record_selector, out.fields, out.source, out.misses = records, fields, source, misses
        fill = ", ".join(f"{n} {rates[n]:.0%}" for n in required) if rows else "no rows"
        out.attempts.append(
            f"attempt {attempt + 1}: records {records!r}"
            + (f" ({matched} matched)" if not is_json else "")
            + "; "
            + (
                failure
                or f"{rows} probed row(s); fill: {fill}; misses: {', '.join(misses) or 'none'}"
                + (f"; filtering records lacking {', '.join(keep)}" if keep else "")
            )
        )
        emit(ReasonEvent(stage="author_extract", text=out.attempts[-1]))
        if source:
            emit(ReasonEvent(stage="author_extract", text=f"attempt {attempt + 1} query: {source}"))
        if not failure and rows and not misses:
            break
        hints = _hints(doc, records, matched, specs, misses, failure)
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
        if not final.rows and not final.report.failures:
            out.attempts.append(
                "the final run returned 0 row(s)"
                + (" -- the where predicate kept no record" if where else "")
            )
        emit(ReasonEvent(stage="author_extract", text=f"final run: {final.report.summary()}"))
    return out
