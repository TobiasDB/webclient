"""The authoring LOOP -- ``Reference + Brief -> the tested query`` -- as a bounded agent loop whose
memory is a CONVERSATION with the model.

The page is sent ONCE: the opening turn is the patterns guide + the ask + the page's flags + ONE
clipped skeleton (:func:`~web.onboard.patterns.author_prompt`, cache-marked on a
:class:`~web.onboard.llm.Conversational` model). Every later turn is a SHORT follow-up that names
exactly what to change -- so a repair costs a few hundred tokens and a cache read, never a re-send
of the page. (A plain ``complete``-only model has no memory; then the opening rides along each
turn -- the fallback, same semantics.)

Each round of the :class:`~web.onboard.agent.BoundedLoop` SAMPLES the current query against the
fetched source (bounded by a wall clock) and the observation drives the next turn:

  * **check**  -- an ADVISORY note on whether the data looks present + on-entity (never a veto:
    a skeleton read is unreliable; absence is concluded EMPIRICALLY, 0 rows after repair),
  * **base**   -- author the records on the reference,
  * **repair** -- the reply didn't parse / the query ran but extracted nothing / a required field
    is empty / the reviewer rejected the sample -> a follow-up with the ONE-LINE reason and a
    targeted hint (a bounded repair budget; the same required field empty twice running means it
    is genuinely ABSENT from the source -> keep the partial, stop burning turns),
  * **deepen** -- a required field lives on each record's DETAIL page: fetch one, add its skeleton
    as a follow-up, re-author with a per-record ``.resolve()`` (the DSL's link fan-out),
  * **split**  -- the reviewer says the rest of the dataset is on a SIBLING page: finalise this
    section and author that page as a new one (the runner concatenates every section),
  * **done**   -- the sampled rows carry every required field (and the reviewer, if any, accepts).

The output is a :class:`~web.onboard.models.QueryArtifact` -- the runnable query plus its
validation (tested / complete / rows / a sample / the rejection trail / absent fields / a
timeliness FLAG) -- so a caller can ship it or say precisely why it isn't ready.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic import JsonValue
from web.dsl import verbs_of
from web.fetch import ClientPool, WebException, emit
from web.parse import Document
from web.resolve import EscalationPolicy, Resolver, flags
from web.resolve import profiles as _rp

from .agent import BoundedLoop, Done, Verdict
from .author import AuthorEvent
from .author_steps import StepSession, run_steps, suggest_selectors
from .compile import Query, QueryError, parse_query, reroot
from .evaluate import skeleton_for
from .llm import Conversation, Conversational, Llm, ReasonEvent
from .models import DatasetBrief, QueryArtifact, QuerySection, Reference
from .patterns import author_prompt, field_schema
from .timeliness import timeliness

#: the authoring ENGINES: ``chain`` asks for the whole ``wq`` chain per turn (the default);
#: ``steps`` builds it one op at a time with per-step feedback (:mod:`.author_steps`).
Engine = Literal["chain", "steps"]
ENGINES: "tuple[Engine, ...]" = ("chain", "steps")
#: the default wall clock per engine (``budget_s=0``): the step engine makes one model call per op,
#: so it needs several times the room of a whole-chain turn.
_DEFAULT_BUDGET_S: "dict[str, float]" = {"chain": 240.0, "steps": 900.0}

#: hard wall-clock cap on running ONE authored query against the source: a pathological query (a
#: per-record .resolve() fanning out to hundreds of fetches) must never hang the loop.
_QUERY_TEST_TIMEOUT = 45.0
#: how many sample rows to keep for the review / the artifact preview.
_SAMPLE = 5


def _fetch_resolver(reference: Reference, pool: ClientPool) -> Resolver:
    """The transport LOCATE determined (``reference.profile``) as a FIXED single tier -- a FETCH, not
    the escalation ladder: choosing the transport is Locate's job, done. The author writes the query
    over exactly what this fetch returns; if the dataset is not there, LOCATE picked the wrong source
    / profile, not the author. Shares the pool (so a browser tier is reused, not relaunched)."""
    prof = _rp.get(reference.profile or "basic") or _rp.BASIC
    base = prof.escalation.tiers[:1] if prof.escalation else ()
    return Resolver(escalation=EscalationPolicy(tiers=base), pool=pool)


@dataclass
class AuthorState:
    """What the loop drives: the source + brief, the clients, the conversation, and the query as it
    grows -- plus the last sample, the rejection trail, and the per-section results."""

    reference: Reference
    brief: DatasetBrief
    resolver: Resolver
    llm: Llm
    #: an optional reviewer model (usually the same model): when set, the loop runs the advisory
    #: entry check and REVIEWS each sample's rows against the brief + entity (a rejection drives a
    #: repair; an incompleteness may point to a sibling page). ``None`` skips both.
    review: "Llm | None" = None
    entity: str = ""
    #: which authoring ENGINE writes the query (see :data:`ENGINES`).
    engine: Engine = "chain"
    #: the conversation with the author model for the CURRENT section (``None`` = a stateless model,
    #: or not opened yet); ``opened`` = the opening (guide + skeleton) has been sent.
    conv: "Conversation | None" = None
    opened: bool = False
    #: the fetched source (set by the check turn) and, for the ``steps`` engine, its session --
    #: the draft + conversation that carry over every author turn of the current section.
    doc: "Document | None" = None
    steps: "StepSession | None" = None
    query: "Query | None" = None
    #: the FINISHED earlier sections authored on THIS page (a split dataset: an Upcoming tab and a
    #: Past list with different record shapes) -- ``query`` is the last; the runner concatenates.
    page_sections: "list[tuple[Query, list[object]]]" = field(default_factory=list)
    #: the DSL verbs the model reached for (name -> count, every attempt) and the ones the DSL does
    #: not have -- the record of the gaps between what a model wants and what the surface offers.
    verbs: "Counter[str]" = field(default_factory=Counter)
    unknown_verbs: "list[str]" = field(default_factory=list)
    rows: "list[object]" = field(default_factory=list)  # the first few rows (review / preview)
    rows_full: "list[object]" = field(default_factory=list)  # every row (artifact / timeliness)
    listing_skeleton: str = ""
    detail_skeleton: str = ""
    nested: bool = False
    #: SPLIT-SOURCE: the FINISHED per-section queries (+ their row counts / rows); ``sibling`` is a
    #: page the model named for the rest of the dataset; ``tried`` guards re-visits.
    sections: "list[tuple[Query, list[object]]]" = field(default_factory=list)
    sibling: str = ""
    tried: "set[str]" = field(default_factory=set)
    offer_sibling: bool = False
    checked: bool = False
    check_note: str = ""
    #: the last FAILURE (a parse reject, a select-miss, an empty sample, an empty required field, a
    #: review rejection) -- the one-line reason for the log; ``hint`` is the fuller guidance for the
    #: model's follow-up. Cleared once repaired.
    last_error: str = ""
    hint: str = ""
    attempts: "list[str]" = field(default_factory=list)  # the rejection trail (one line each)
    repairs: int = 0
    max_repairs: int = 3
    #: required fields left empty by the PREVIOUS attempt (records matched) -> the same ones empty
    #: again means they are genuinely ABSENT from the source: stop re-authoring, keep the partial.
    prev_missing: "set[str]" = field(default_factory=set)
    absent: "set[str]" = field(default_factory=set)
    #: the reviewer's LAST verdict on the final sample: ``""`` = accepted (or no reviewer), else
    #: its rejection -- carried on the artifact so "ready" never hides a rejected sample.
    review_note: str = ""
    #: a DEFINED stop decided from ground truth before any authoring turn (``js_gated: ...`` when
    #: the fetched page's own signals say JS-app and it carries no records at the HTTP tier):
    #: the loop ends without guessing selectors, and the artifact carries this as its reason.
    abort: str = ""


@dataclass
class _Obs:
    phase: str  # "check" | "base" | "extend" | "split"
    missing: "list[str]" = field(default_factory=list)
    detail_link: "str | None" = None
    error: str = ""
    rows_empty: bool = False
    can_repair: bool = True
    absent_stop: bool = False  # the same required field(s) stayed empty -> genuinely absent


def _populated(rows: "list[object]") -> "list[object]":
    """The rows that carry at least one non-empty field -- an all-empty row means the record
    selector matched but every field selector missed, which is not extracted data."""
    out: list[object] = []
    for r in rows:
        if isinstance(r, dict):  # reserved identity columns (_key / _doc) do not populate a row
            if any(v not in (None, "", [], {}) for k, v in r.items() if not str(k).startswith("_")):
                out.append(r)
        elif r not in (None, "", [], {}):
            out.append(r)
    return out


def _present_keys(value: object, out: "set[str]") -> None:
    """Every key carrying a non-empty value, at ANY depth -- a field extracted inside a nested
    branch (a detail-page fan-out: ``detail={body: ...}``) counts as present."""
    if isinstance(value, dict):
        for k, v in value.items():
            if str(k).startswith("_"):  # a reserved column (_key / _doc identity) is not a field
                continue
            if v not in (None, "", [], {}):
                out.add(str(k))
            _present_keys(v, out)
    elif isinstance(value, list):
        for v in value:
            _present_keys(v, out)


def _missing(brief: DatasetBrief, rows: "list[object]") -> "list[str]":
    """The brief's REQUIRED fields empty (or absent) in every sampled row (nested branches count)."""
    present: set[str] = set()
    for row in rows:
        _present_keys(row, present)
    return [f for f in brief.fields if f not in present and f not in brief.optional]


def _record_link(rows: "list[object]") -> "str | None":
    """A per-record URL in the sample to follow for detail (the first http(s) value)."""
    for row in rows:
        if isinstance(row, dict):
            for v in row.values():
                if isinstance(v, str) and v.startswith(("http://", "https://")):
                    return v
    return None


def _yes(reply: str) -> bool:
    """A YES/NO verdict from the first non-empty line (default accept on an odd reply)."""
    first = next((ln for ln in reply.strip().splitlines() if ln.strip()), "").strip().lower()
    return first.startswith("yes") or (not first.startswith("no") and "yes" in first)


def _scope(state: AuthorState) -> str:
    return f" It must be {state.entity}'s OWN data." if state.entity else ""


async def _test_query(query: Query, resolver: Resolver) -> "tuple[bool, list[object]]":
    """Run the query against the source, HARD-BOUNDED by :data:`_QUERY_TEST_TIMEOUT` -> ``(ran,
    rows)``. A transport/select failure or a timeout is ``(False, [])`` with the reason raised as a
    :class:`WebException` by the caller's handling."""
    result = await asyncio.wait_for(query.acollect(resolver=resolver), timeout=_QUERY_TEST_TIMEOUT)
    rows: list[object] = list(result) if isinstance(result, list) else [result]
    return True, rows


async def _check_source(state: AuthorState) -> "tuple[bool, str]":
    """Entry guard (ADVISORY): does the located page actually HOLD this dataset (records present,
    on-entity)? ``review`` off -> always OK (no model call)."""
    if state.review is None:
        return True, ""
    want = state.brief.goal or "the target dataset"
    schema = (
        ("\nEach record should have: " + ", ".join(state.brief.fields))
        if state.brief.fields
        else ""
    )
    prompt = (
        f"You are verifying a page holds a dataset BEFORE extracting it. Dataset: {want}.{schema}"
        f"{_scope(state)}\n\nPage ({state.reference.url}):\n{state.listing_skeleton}\n\n"
        "Is this dataset actually PRESENT on THIS page (its records visible in the skeleton, not an "
        "empty section, a login wall, or an unfilled iframe)? Answer YES or NO on the first line, "
        "then one short reason."
    )
    reply = await state.review.complete(prompt)
    emit(ReasonEvent(stage="check", subject=state.reference.url, text=reply.strip()))
    return _yes(reply), reply.strip()


#: per-VALUE cap in a review sample: every column stays visible (a long nested body no longer
#: pushes the headline / url off the end of a whole-JSON cut and gets them called "missing").
_PREVIEW_VALUE_CHARS = 160
_PREVIEW_CHARS = 3_000


def _clip_value(value: object) -> object:
    if isinstance(value, str):
        if len(value) <= _PREVIEW_VALUE_CHARS:
            return value
        # an explicit, unmistakable display cut -- a bare "…" was read as a truncated extraction
        return (
            value[:_PREVIEW_VALUE_CHARS]
            + f" [+{len(value) - _PREVIEW_VALUE_CHARS} more chars, clipped for display]"
        )
    if isinstance(value, dict):  # reserved identity columns are not for the reviewer
        return {k: _clip_value(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, list):
        head = [_clip_value(v) for v in value[:3]]
        return head + (
            [f"[+{len(value) - 3} more items, clipped for display]"] if len(value) > 3 else []
        )
    return value


def _preview(rows: "list[object]") -> str:
    """The sample rows as JSON with every LEAF clipped (long strings, long lists) -- so a reviewer
    sees each column of each row, and the whole stays within :data:`_PREVIEW_CHARS`."""
    text = json.dumps([_clip_value(r) for r in rows], ensure_ascii=False, default=str)
    return text if len(text) <= _PREVIEW_CHARS else text[:_PREVIEW_CHARS] + "…"


async def _review_rows(state: AuthorState) -> "tuple[bool, str]":
    """Per-sample review: do the rows really match the brief + entity, with real values and the
    WHOLE dataset the brief asks for (its ``review_hint`` is the brief-specific strictness)?"""
    if state.review is None or not state.rows:
        return True, ""
    want = state.brief.goal or "the target dataset"
    schema = "\n".join(field_schema(state.brief)) if state.brief.fields else "(the salient fields)"
    sample = _preview(state.rows[:_SAMPLE])
    extra = (
        f"\nBrief-specific check (be strict on this): {state.brief.review_hint}"
        if state.brief.review_hint
        else ""
    )
    if state.absent:  # a field the source does not carry is not a defect of the sample
        extra += (
            f"\nFields established as ABSENT from this source (do NOT fail the sample for them): "
            + ", ".join(sorted(state.absent))
        )
    prompt = (
        f"You are reviewing extracted sample rows against a brief. Dataset: {want}.{_scope(state)}\n"
        f"Schema:\n{schema}{extra}\n\nSample rows (JSON; long values are CLIPPED for display -- "
        f"'[+N more chars, clipped for display]' marks the preview cut, NOT a truncated "
        f"extraction; the full value was extracted):\n{sample}\n\n"
        "Do these rows correctly match the brief -- the right entity, real values (not nulls or raw "
        "markup), every NON-optional field populated, and any brief-specific check above satisfied? "
        "A field marked (optional) may be empty or missing -- that is never a reason to reject. "
        "Judge what a value IS, not its displayed length. Answer YES or NO on the first line, then "
        "one short reason naming exactly what is wrong or missing."
    )
    reply = await state.review.complete(prompt)
    emit(ReasonEvent(stage="review", text=reply.strip()))
    return _yes(reply), reply.strip()


def _opening(state: AuthorState) -> str:
    """The opening turn: guide + ask + flags + the (clipped) listing skeleton -- sent ONCE."""
    detail = state.reference.detail
    recency_bits = [
        (
            f"the records are {detail['sort_order']}"
            if isinstance(detail.get("sort_order"), str) and detail.get("sort_order")
            else ""
        ),
        str(detail.get("recency_hint") or ""),
    ]
    recency = "; ".join(b for b in recency_bits if b)
    return author_prompt(
        state.brief,
        state.listing_skeleton,
        list(state.reference.assessment),
        kind=state.reference.kind,
        detail=bool(state.detail_skeleton),
        recency=recency,
        record_selector=state.reference.record_selector or "",
    )


def _follow_up(state: AuthorState) -> str:
    """The SHORT follow-up for a repair / deepen / split turn: what failed and what to change --
    never the page again (it is in the conversation's cached opening)."""
    steps = state.engine == "steps"
    parts: list[str] = []
    if state.check_note and not state.opened:  # surfaced once, with the opening
        parts.append(f"NOTE from a reviewer of this page: {state.check_note}")
    if state.detail_skeleton and state.nested:
        parts.append(
            "Some required fields are NOT on the listing -- they are on each record's DETAIL page. "
            "A sample detail page (linked from one record) skeleton:\n"
            f"{state.detail_skeleton}\n\n"
            + (
                'Follow the record\'s link with detail("<link css>") and add each of those fields '
                "with detail_field(<name>, <chain>) -- there wq.doc IS the detail page."
                if steps
                else "Re-write the query so those fields FOLLOW each record's link ONCE and fan out: "
                "detail=wq.doc.select('<link>').attr('href').resolve().extract(<field>=wq.doc.select("
                "'...').attr('text'), ...) -- inside that extract, wq.doc IS the detail page. Never "
                "repeat the select/resolve per field."
            )
        )
    if state.last_error:
        prior = state.query.describe() if state.query is not None else "(no parseable query yet)"
        parts.append(
            f"Your PREVIOUS query FAILED and must be fixed:\n{prior}\nFAILURE: {state.last_error}"
            + (f"\n{state.hint}" if state.hint else "")
            + (
                "\nFix it op by op: records(...) re-picks the record selector; field(...) replaces "
                "a column; drop(...) removes one -- then done()."
                if steps
                else "\nWrite a CORRECTED wq.doc... chain -- a MATERIALLY different one where the "
                "record selector missed; a field that may be absent on some records must be optional "
                "(select(css, optional=True)); do NOT use a Python dict literal."
            )
        )
    if state.offer_sibling:
        parts.append(
            "If the MISSING records are not on THIS page but on a SEPARATE sibling page (e.g. this is "
            "the PAST/archived page and the UPCOMING records are at a different URL, or vice versa), "
            "reply with EXACTLY `SIBLING: <that full url>` (nothing else) and it will be extracted "
            "separately and combined. Only when the records are genuinely on another page, not merely "
            "a section lower on THIS one."
        )
    if not steps:
        parts.append("Reply with ONLY the wq.doc... chain (or a single SIBLING: line) -- no prose.")
    return "\n\n".join(parts)


async def _send(state: AuthorState, opening: str, follow_up: str) -> str:
    """One authoring turn -> the model's raw reply. On a conversational model: the opening ONCE
    (cache-marked), then just the follow-up. On a stateless model: the opening rides along."""
    if state.conv is None and isinstance(state.llm, Conversational):
        state.conv = state.llm.conversation()
    if state.conv is not None:
        text = opening if not state.opened else follow_up
        state.opened = True
        return await state.conv.send(text)
    state.opened = True
    return await state.llm.complete(opening if not follow_up else f"{opening}\n\n---\n{follow_up}")


async def _author(state: AuthorState) -> None:
    """Author (or re-author) the current section's query -- one model turn. Sets ``state.query`` on
    a parseable reply; a parse REJECT records the reason for a repair turn. A ``SIBLING:`` reply
    (only when offered) schedules a split. The raw reply is emitted before parse. On the ``steps``
    engine the turn is the step loop instead (:func:`~web.onboard.author_steps.run_steps`): the
    follow-up becomes its next turn and the draft carries over."""
    follow = _follow_up(state) if (state.opened or state.check_note or state.last_error) else ""
    if state.engine == "steps":
        await _author_steps(state, follow)
        return
    opening = _opening(state)
    reply = await _send(state, opening, follow)
    emit(AuthorEvent(phase="reply", reply=reply))
    stripped = reply.strip()
    if state.offer_sibling and stripped.upper().startswith("SIBLING:"):
        url = stripped.split(":", 1)[1].strip().split()[0] if ":" in stripped else ""
        if url.startswith(("http://", "https://")) and url not in state.tried:
            state.sibling = url
            state.last_error = state.hint = ""
            emit(ReasonEvent(stage="author", subject=url, text="a sibling page holds the rest"))
            return
    profile = state.reference.profile or None
    segments = _sections(reply)
    queries: list[Query] = []
    for i, seg in enumerate(segments):
        try:
            expr = parse_query(seg)
        except QueryError as exc:  # unparseable / disallowed -> a repair turn re-authors with this
            _tally(state, exc.verbs, exc.unknown)
            where = f"section {i + 1} of {len(segments)}: " if len(segments) > 1 else ""
            reason, state.hint = _parse_diagnosis(seg, exc)
            state.last_error = where + reason
            emit(ReasonEvent(stage="author", text=f"query rejected: {state.last_error}"))
            return
        _tally(state, verbs_of(expr), ())
        queries.append(reroot(expr, state.reference.url, profile=profile))
    state.page_sections = []
    for q in queries[:-1]:  # a split dataset: test the earlier sections now; the last is sampled
        try:
            _ran, rows = await _test_query(q, state.resolver)
        except (WebException, asyncio.TimeoutError) as exc:
            state.last_error = f"section {q.describe()} failed to run: {exc}"
            state.hint = "Fix that section's selectors (or drop the section if it is empty)."
            emit(ReasonEvent(stage="author", text=f"query rejected: {state.last_error}"))
            return
        state.page_sections.append((q, rows))
        emit(ReasonEvent(stage="author", text=f"section {q.describe()} → {len(rows)} row(s)"))
    state.query = queries[-1]
    state.last_error = state.hint = ""


#: the line that separates SECTION queries in a reply (a split dataset: one chain per section).
_SECTION_SEP = re.compile(r"^\s*---+\s*$", re.MULTILINE)


def _sections(reply: str) -> "list[str]":
    """The reply split into its section chains on ``---`` lines (segments without ``wq.`` dropped);
    one segment when there is no separator."""
    parts = [p for p in _SECTION_SEP.split(reply) if "wq." in p]
    return parts or [reply]


def _tally(state: AuthorState, verbs: "Sequence[str]", unknown: "Sequence[str]") -> None:
    state.verbs.update(verbs)
    for v in unknown:
        if v not in state.unknown_verbs:
            state.unknown_verbs.append(v)


async def _author_steps(state: AuthorState, note: str) -> None:
    """The ``steps`` engine's author turn: drive the step loop over the section's session (opened
    on the first turn over the fetched document) with ``note`` as its next turn."""
    if state.steps is None:
        doc = state.doc or await state.resolver.resolve(state.reference.url)
        state.doc = doc
        state.steps = StepSession(doc=doc)
    state.steps.offer_sibling = state.offer_sibling
    state.opened = True
    detail = state.reference.detail
    recency = "; ".join(
        b
        for b in (
            (
                f"the records are {detail['sort_order']}"
                if isinstance(detail.get("sort_order"), str) and detail.get("sort_order")
                else ""
            ),
            str(detail.get("recency_hint") or ""),
        )
        if b
    )
    result = await run_steps(
        state.steps,
        reference=state.reference,
        brief=state.brief,
        resolver=state.resolver,
        llm=state.llm,
        flags=list(state.reference.assessment),
        recency=recency,
        note=note,
    )
    if result.sibling and state.offer_sibling and result.sibling not in state.tried:
        state.sibling = result.sibling
        state.last_error = state.hint = ""
        emit(
            ReasonEvent(
                stage="author", subject=result.sibling, text="a sibling page holds the rest"
            )
        )
        return
    if result.note:
        state.attempts.append(result.note)
    _tally(state, [], state.steps.unknown)
    state.verbs = Counter(state.steps.verbs)  # the session's running tally IS this page's
    if result.absent - state.absent:
        declared = sorted(result.absent - state.absent)
        state.attempts.append(f"field(s) {', '.join(declared)} declared absent by the model")
    state.absent |= result.absent
    state.page_sections = list(result.sections)
    state.query = result.query
    state.last_error, state.hint = result.error, result.hint
    if result.query is not None:
        emit(AuthorEvent(phase="reply", reply=result.query.describe()))


def _parse_diagnosis(reply: str, exc: QueryError) -> "tuple[str, str]":
    """``(one-line reason, hint)`` for a reply that was NOT a valid wq query: the parser's error plus
    WHAT the model actually replied (a snippet) -- so the log shows the cause, not just "invalid
    syntax" -- and a hint targeted at the most likely mistake."""
    text = reply.strip()
    full = " ".join(text.split())  # the WHOLE reply -- a human must be able to see why
    code = text[text.find("wq.") :] if "wq." in text else ""
    if exc.unknown:
        why, hint = (
            f"unknown DSL verb(s) {', '.join(exc.unknown)} -- the DSL has no such verb",
            f"The verb(s) {', '.join(exc.unknown)} do not exist. Use ONLY the verbs in the guide "
            "(select / select_all / attr / text / number / date / datetime / split / map / link / "
            "regex / resolve / extract / filter / limit / is_ok / is_empty / project); a list of "
            "values is a select_all(...).attr('text') column -- there is no join.",
        )
    elif not code:
        why, hint = (
            "no wq chain in the reply (prose instead of code)",
            "You replied with prose. Reply with ONLY the query code -- one wq.doc... chain, nothing "
            "else (no explanation, no code fence). If the data is genuinely not on this page, say "
            "exactly: SIBLING: <url> only when offered, else still write the best chain you can.",
        )
    elif "{" in code or "}" in code:
        why, hint = (
            "a Python dict literal in the query (not allowed)",
            "Do NOT use a dict literal ({...}). Each extract column is a wq.doc chain: "
            "extract(name=wq.doc.select('.x').attr('text'), ...). For a nested branch, use a nested "
            "extract(...) call, not a dict.",
        )
    elif code.count("(") != code.count(")") or code.count("[") != code.count("]"):
        why, hint = (
            "unbalanced parentheses/brackets -- the chain was cut off or mis-nested",
            "Check every select(...) / extract(...) / attr(...) is closed and the chain ends with "
            ".project(). Write the complete chain on one logical expression.",
        )
    elif "```" in text:
        why, hint = (
            "a code fence around the query",
            "Reply with the bare chain -- no ``` fences, no language tag, no prose.",
        )
    elif code.lstrip().startswith(("wq.reference", "wq.fetch", "wq.resolve")):
        why, hint = (
            "the chain is rooted at a reference/fetch, not at wq.doc",
            "Root the query at wq.doc -- the pipeline supplies the source; do NOT wrap it in "
            "wq.reference(...)/.resolve() (a per-record .attr('href').resolve() INSIDE extract() is "
            "fine).",
        )
    else:
        why, hint = (
            f"{exc}",
            "Reply with ONLY query code -- one wq.doc... chain (or, for a split dataset, one chain "
            "per section separated by a line containing only ---). Use only the syntax in the guide.",
        )
    return f"the reply was not a valid wq query: {why}\n  the reply was: {full!r}", hint


def _fail_reason(state: AuthorState, rows: "list[object]") -> "tuple[str, str]":
    """``(one-line reason, hint)`` for a query that RAN but did not extract the dataset -- the
    reason for the log, the fuller hint for the model's follow-up."""
    good = _populated(rows)
    if not good:
        return (
            "0 populated rows -- the record selector (select_all) matched nothing or every field "
            "selector missed",
            "Pick a DIFFERENT repeating element from the skeleton for .select_all(...) (the one "
            "marked ← RECORD LIST is the likely row), and read each field with a selector that is "
            "IN that record. If the records are not visible in the skeleton at all, the page is "
            "client-rendered and a static query cannot reach them -- do not guess.",
        )
    empty = _missing(state.brief, good)
    cols = ", ".join(empty)
    return (
        f"required field(s) {cols} empty on every row",
        f"The field(s) {cols} came back EMPTY on every row: their selector matched no element, or "
        "matched one whose TEXT is empty because the value is in an ATTRIBUTE (read it with "
        '.attr("<name>")). If a field only exists on the record\'s own page, follow its link with '
        ".attr('href').resolve() and select it there.",
    )


async def _observe(state: AuthorState) -> _Obs:
    if state.abort:
        return _Obs(phase="abort")
    if state.sibling:
        return _Obs(phase="split")
    if not state.checked:
        return _Obs(phase="check")
    if state.query is None and not state.last_error:
        return _Obs(phase="base")
    if state.last_error:  # the author just REJECTED the reply -- nothing to sample
        return _Obs(
            phase="extend",
            error=state.last_error,
            rows_empty=True,
            can_repair=state.repairs < state.max_repairs,
        )
    assert state.query is not None
    try:  # SAMPLE: run the current query (bounded)
        _ran, rows = await _test_query(state.query, state.resolver)
        state.rows = rows[:_SAMPLE]
        state.rows_full = rows
    except WebException as exc:  # a select-miss / transport failure -> feed it back
        state.rows, state.rows_full = [], []
        state.last_error = f"running the query failed -- {exc.error.code}: {exc.error.message}"
        state.hint = "Fix the selector that missed (a required select() must match every record)."
        missed = exc.error.detail.get("selector") if exc.error.code == "dsl.select_miss" else None
        if isinstance(missed, str) and state.doc is not None:  # the closest selectors on the page
            scope = state.doc.select(state.reference.record_selector or "") or state.doc
            close = suggest_selectors(scope, missed)
            if close:
                state.hint += f" Closest selectors in the record: {', '.join(close)}."
    except asyncio.TimeoutError:
        state.rows, state.rows_full = [], []
        state.last_error = f"the query test exceeded {_QUERY_TEST_TIMEOUT:.0f}s and was cancelled"
        state.hint = "Avoid a per-record .resolve() over many rows unless a field truly needs it."
    good = _populated(state.rows)
    missing = _missing(state.brief, good) if good else []
    link = _record_link(good) if (missing and not state.nested) else None
    if not state.last_error and good and not missing and state.review is not None:
        ok, note = await _review_rows(state)  # a real, complete sample -> REVIEW it
        state.review_note = "" if ok else note
        if not ok:
            state.last_error = f"the sample does not satisfy the brief: {note}"
            state.hint = ""
            state.offer_sibling = True  # incompleteness -> the repair may point to a sibling page
    if not state.last_error and (not good or (missing and link is None)):
        reason, hint = _fail_reason(state, state.rows)
        state.last_error, state.hint = reason, hint
    # a required field the model CANNOT populate is either a wrong selector or a field that is
    # genuinely not on the page -- the same field(s) empty on TWO successive attempts (records
    # matched, other fields fine) means absent: stop re-authoring it, keep the best partial.
    absent_stop = bool(good and missing and link is None and set(missing) <= state.prev_missing)
    state.prev_missing = set(missing) if good else state.prev_missing
    return _Obs(
        phase="extend",
        missing=missing,
        detail_link=link,
        error=state.last_error,
        rows_empty=not good,
        can_repair=state.repairs < state.max_repairs,
        absent_stop=absent_stop,
    )


async def _decide(obs: _Obs) -> "str | Done":
    if obs.phase == "abort":
        return Done()  # a defined stop -- nothing to author
    if obs.phase == "split":
        return "split"
    if obs.phase == "check":
        return "check"
    if obs.phase == "base":
        return "base"
    if obs.missing and obs.detail_link is not None:  # fields on a linked detail page -> deepen
        return "deepen"
    if obs.absent_stop:
        return "absent"
    if obs.error or obs.rows_empty:  # a failed/empty/mismatched query -> repair while budget lasts
        return "repair" if obs.can_repair else Done()
    return Done()  # every required field is present (or unreachable) -- stop


async def _apply(state: AuthorState, turn: "str | Done") -> None:
    if turn == "split":
        # finalise the current section, then re-target the loop at the sibling page as a NEW section
        # (a fresh check -> base there, in a fresh conversation over THAT page's skeleton).
        state.sections.extend(state.page_sections)
        state.page_sections = []
        if state.query is not None:
            state.sections.append((state.query, list(state.rows_full)))
        state.tried.add(state.reference.url)
        state.tried.add(state.sibling)
        state.reference = state.reference.model_copy(
            update={"url": state.sibling, "page_url": state.sibling}
        )
        emit(
            ReasonEvent(
                stage="author", subject=state.sibling, text="authoring the sibling section next"
            )
        )
        state.sibling = ""
        state.query = None
        state.conv, state.opened = None, False
        state.doc, state.steps = None, None
        state.checked = False
        state.check_note = state.last_error = state.hint = ""
        state.offer_sibling = state.nested = False
        state.detail_skeleton = ""
        state.repairs = 0
        state.prev_missing = set()
    elif turn == "check":
        sample = await state.resolver.resolve(state.reference.url)
        state.doc = sample  # the ONE fetched source (the steps engine probes every op against it)
        state.listing_skeleton = skeleton_for(sample)  # ONE clipped skeleton, reused by every turn
        fired = list(flags(sample))
        state.reference = state.reference.model_copy(
            update={"kind": sample.kind, "assessment": fired}
        )
        by = {f.name for f in fired if f.present}
        # GROUND TRUTH before any model turn: the page we actually fetched (at Locate's tier) says
        # it is a JS app AND carries no record region -> a static query cannot reach the data, so
        # authoring would only guess selectors at a shell. That is a Locate mis-tiering, not an
        # authoring problem: stop with a defined reason instead of burning turns.
        js_app = (
            any(  # the JS-app SIGNAL, not the `needs_browser` conclusion (fires on `empty` too)
                f.name in ("spa", "iframe") or any(s.name == "spa" for s in f.signals)
                for f in fired
                if f.present
            )
        )
        on_http = (state.reference.profile or "basic") == "basic"
        if js_app and on_http and not sample.records(top_k=1):
            state.checked = True
            state.abort = (
                f"js_gated: the fetched page's signals say JS-app ({', '.join(sorted(by))}) and it "
                "holds no record region at the HTTP tier -- a static query cannot reach the data"
            )
            emit(
                ReasonEvent(
                    stage="check",
                    subject=state.reference.url,
                    text="JS-gated at the HTTP tier (the page's own signals) — not authoring; "
                    "re-run locate (it should bake a browser profile) or force --full-browser",
                )
            )
            return
        ok, note = await _check_source(state)
        state.checked = True
        if not ok:  # advisory only -- attempt anyway; a skeleton read is not a reliable veto
            state.check_note = note
            basic = (state.reference.profile or "basic") == "basic"
            hint = (
                " — the page is on the HTTP tier but looks JS-gated; if extraction comes back empty, "
                "re-run locate (it should bake a browser profile) or force --full-browser"
                if basic
                else ""
            )
            emit(
                ReasonEvent(
                    stage="check", text=f"concern (advisory, attempting anyway): {note}{hint}"
                )
            )
    elif turn == "base":
        emit(ReasonEvent(stage="author", text="authoring the base query for the listed records"))
        await _author(state)
    elif turn == "repair":
        state.repairs += 1
        state.attempts.append(f"attempt {state.repairs}: {state.last_error}")
        emit(
            ReasonEvent(
                stage="author", text=f"repair {state.repairs}: {state.last_error} — retrying"
            )
        )
        await _author(state)
    elif turn == "deepen":
        link = _record_link(_populated(state.rows))
        if link is None:
            return
        state.nested = True
        if (
            state.engine == "steps"
        ):  # the step engine fetches + shows the detail page on detail(...)
            lacking = ", ".join(_missing(state.brief, _populated(state.rows)))
            state.detail_skeleton = ""
            state.last_error = f"required field(s) {lacking} are not on the listing records"
            state.hint = (
                'They are on each record\'s own page: follow its link with detail("<link css>") '
                "and add each with detail_field(<name>, <chain>)."
            )
        else:
            detail = await state.resolver.resolve(link)
            state.detail_skeleton = skeleton_for(detail)
            state.last_error = state.hint = ""
        emit(
            ReasonEvent(
                stage="author",
                subject=link,
                text="the brief needs per-record detail -- deepening with a detail-page extraction",
            )
        )
        await _author(state)
    elif turn == "absent":
        missing = _missing(state.brief, _populated(state.rows))
        state.absent |= set(missing)
        state.attempts.append(f"field(s) {', '.join(sorted(missing))} absent from the source")
        emit(
            ReasonEvent(
                stage="author",
                text=f"field(s) {', '.join(sorted(missing))} are absent from the source (records + "
                "other fields extract cleanly) — keeping the partial, no more retries",
            )
        )
        state.last_error = state.hint = ""  # keep the partial as the section's query


def _json_rows(rows: "list[object]") -> "list[JsonValue]":
    """The rows as JSON values (what the DSL's collect yields); a non-JSON item is dropped."""
    out: list[JsonValue] = []
    for r in rows:
        if r is None or isinstance(r, (dict, list, str, int, float)):
            out.append(r)
    return out


def _artifact(state: AuthorState, verdict: Verdict) -> QueryArtifact:
    """Fold the loop's outcome into the :class:`QueryArtifact` (see the module docstring)."""
    parts = _parts(state)
    all_rows: list[object] = [r for _, rows in parts for r in _populated(rows)]
    json_rows = _json_rows(all_rows)
    tested = bool(parts) and all(len(rows) > 0 for _, rows in parts)
    missing = _missing(state.brief, all_rows) if all_rows else list(state.brief.fields)
    tnote, stale = timeliness(json_rows, state.brief)
    present: set[str] = set()
    for row in all_rows:
        _present_keys(row, present)
    absent = sorted(state.absent - present)  # a field that ended up populated is not absent
    primary = parts[0][0] if parts else None
    reason = state.abort or (verdict.reason + (f": {verdict.error}" if verdict.error else ""))
    return QueryArtifact(
        blob=primary.to_blob() if primary is not None else "",
        describe=primary.describe() if primary is not None else "",
        tested=tested,
        complete=bool(tested and all_rows and not [m for m in missing if m not in absent]),
        row_count=len(all_rows),
        sample=json_rows[:_SAMPLE],
        sections=(
            [
                QuerySection(
                    blob=q.to_blob(), describe=q.describe(), row_count=len(_populated(rows))
                )
                for q, rows in parts
            ]
            if len(parts) > 1
            else []
        ),
        attempts=list(state.attempts),
        absent=absent,
        timeliness=tnote,
        stale=stale,
        reason=reason,
        verbs=dict(state.verbs.most_common()),
        unknown_verbs=list(state.unknown_verbs),
        review=" ".join(state.review_note.split()),
    )


def _parts(state: AuthorState) -> "list[tuple[Query, list[object]]]":
    """Every section query with its rows: the sibling pages' sections, then this page's earlier
    sections, then the current query."""
    return [
        *state.sections,
        *state.page_sections,
        *([(state.query, list(state.rows_full))] if state.query is not None else []),
    ]


async def _run(
    reference: Reference,
    brief: DatasetBrief,
    *,
    resolver: Resolver,
    llm: Llm,
    review: "Llm | None",
    entity: str,
    max_rounds: int,
    budget_s: float,
    engine: Engine,
) -> "tuple[AuthorState, Verdict]":
    state = AuthorState(
        reference=reference,
        brief=brief,
        resolver=_fetch_resolver(reference, resolver.pool),
        llm=llm,
        review=review,
        entity=entity,
        engine=engine,
    )
    loop: "BoundedLoop[AuthorState, _Obs, str | Done]" = BoundedLoop(
        observe=_observe,
        decide=_decide,
        apply=_apply,
        done=lambda d: isinstance(d, Done),
        # progress = (#finished sections, current query) so finalising a section counts as progress
        progress=lambda s: (len(s.sections), s.query.to_blob() if s.query is not None else ""),
        max_rounds=max_rounds,
    )
    wall = budget_s if budget_s > 0 else _DEFAULT_BUDGET_S[engine]
    try:
        verdict = await asyncio.wait_for(loop.arun(state), timeout=wall)
    except asyncio.TimeoutError:  # a slow model / stuck page -- take the sections so far
        emit(ReasonEvent(stage="author", text=f"authoring hit the {wall:.0f}s budget — stopping"))
        verdict = Verdict(reason="budget", rounds=state.repairs)
    if verdict.reason == "error" and verdict.error:
        emit(ReasonEvent(stage="author", text=f"authoring aborted — {verdict.error}"))
    preview = json.dumps(state.rows[0], ensure_ascii=False, default=str)[:400] if state.rows else ""
    emit(
        AuthorEvent(
            phase="done", rows=len(_populated(state.rows)), sample=preview, reply=state.last_error
        )
    )
    return state, verdict


async def write_query(
    reference: Reference,
    brief: DatasetBrief,
    *,
    resolver: Resolver,
    llm: Llm,
    review: "Llm | None" = None,
    entity: str = "",
    max_rounds: int = 12,
    budget_s: float = 0.0,
    engine: Engine = "chain",
) -> QueryArtifact:
    """Author the query for ``reference`` per ``brief`` and return the :class:`QueryArtifact`: the
    runnable query + its validation verdict (see the module docstring). ``review`` enables the
    entry check + per-sample review; ``budget_s`` is a hard wall clock for the whole loop (``0`` =
    the engine's default, 240s for ``chain`` / 900s for ``steps``); ``engine`` picks how each
    author turn writes the query (see :data:`ENGINES`)."""
    state, verdict = await _run(
        reference,
        brief,
        resolver=resolver,
        llm=llm,
        review=review,
        entity=entity,
        max_rounds=max_rounds,
        budget_s=budget_s,
        engine=engine,
    )
    return _artifact(state, verdict)


async def author_agent(
    reference: Reference,
    brief: DatasetBrief,
    *,
    resolver: Resolver,
    llm: Llm,
    review: "Llm | None" = None,
    entity: str = "",
    max_rounds: int = 12,
    budget_s: float = 0.0,
    engine: Engine = "chain",
) -> "tuple[list[Query], Verdict]":
    """The section QUERIES (usually one; more for a split dataset) + the loop's
    :class:`~web.onboard.agent.Verdict` -- the seam the CLI and the programmatic entries run on.
    See :func:`write_query` for the full artifact with the validation verdict."""
    state, verdict = await _run(
        reference,
        brief,
        resolver=resolver,
        llm=llm,
        review=review,
        entity=entity,
        max_rounds=max_rounds,
        budget_s=budget_s,
        engine=engine,
    )
    return [q for q, _ in _parts(state)], verdict


__all__ = ["author_agent", "write_query", "AuthorState", "Engine", "ENGINES"]
