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
from dataclasses import dataclass, field

from pydantic import JsonValue
from web.fetch import ClientPool, WebException, emit
from web.resolve import EscalationPolicy, Resolver, flags
from web.resolve import profiles as _rp

from .agent import BoundedLoop, Done, Verdict
from .author import AuthorEvent
from .compile import Query, QueryError, parse_query, reroot
from .evaluate import skeleton_for
from .llm import Conversation, Conversational, Llm, ReasonEvent
from .models import DatasetBrief, QueryArtifact, QuerySection, Reference
from .patterns import author_prompt, field_schema
from .timeliness import timeliness

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
    #: the conversation with the author model for the CURRENT section (``None`` = a stateless model,
    #: or not opened yet); ``opened`` = the opening (guide + skeleton) has been sent.
    conv: "Conversation | None" = None
    opened: bool = False
    query: "Query | None" = None
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
        if isinstance(r, dict):
            if any(v not in (None, "", [], {}) for v in r.values()):
                out.append(r)
        elif r not in (None, "", [], {}):
            out.append(r)
    return out


def _missing(brief: DatasetBrief, rows: "list[object]") -> "list[str]":
    """The brief's REQUIRED fields empty (or absent) in every sampled row."""
    present: set[str] = set()
    for row in rows:
        if isinstance(row, dict):
            for k, v in row.items():
                if v not in (None, "", [], {}):
                    present.add(k)
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


async def _review_rows(state: AuthorState) -> "tuple[bool, str]":
    """Per-sample review: do the rows really match the brief + entity, with real values and the
    WHOLE dataset the brief asks for (its ``review_hint`` is the brief-specific strictness)?"""
    if state.review is None or not state.rows:
        return True, ""
    want = state.brief.goal or "the target dataset"
    schema = "\n".join(field_schema(state.brief)) if state.brief.fields else "(the salient fields)"
    sample = json.dumps(state.rows[:_SAMPLE], ensure_ascii=False, default=str)[:1500]
    extra = (
        f"\nBrief-specific check (be strict on this): {state.brief.review_hint}"
        if state.brief.review_hint
        else ""
    )
    prompt = (
        f"You are reviewing extracted sample rows against a brief. Dataset: {want}.{_scope(state)}\n"
        f"Schema:\n{schema}{extra}\n\nSample rows (JSON):\n{sample}\n\n"
        "Do these rows correctly match the brief -- the right entity, real values (not nulls or raw "
        "markup), every non-optional field populated, and any brief-specific check above satisfied? "
        "Answer YES or NO on the first line, then one short reason naming exactly what is wrong or "
        "missing."
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
    )


def _follow_up(state: AuthorState) -> str:
    """The SHORT follow-up for a repair / deepen / split turn: what failed and what to change --
    never the page again (it is in the conversation's cached opening)."""
    parts: list[str] = []
    if state.check_note and not state.opened:  # surfaced once, with the opening
        parts.append(f"NOTE from a reviewer of this page: {state.check_note}")
    if state.detail_skeleton and state.nested:
        parts.append(
            "Some required fields are NOT on the listing -- they are on each record's DETAIL page. "
            "A sample detail page (linked from one record) skeleton:\n"
            f"{state.detail_skeleton}\n\n"
            "Re-write the query so those fields FOLLOW each record's link: ONE column per detail "
            "field, each `wq.doc.select('<link>').attr('href').resolve().select(...).attr('text')` "
            "(that page is fetched once per record and reused for every column)."
        )
    if state.last_error:
        prior = state.query.describe() if state.query is not None else "(no parseable query yet)"
        parts.append(
            f"Your PREVIOUS query FAILED and must be fixed:\n{prior}\nFAILURE: {state.last_error}"
            + (f"\n{state.hint}" if state.hint else "")
            + "\nWrite a CORRECTED wq.doc... chain -- a MATERIALLY different one where the record "
            "selector missed; a field that may be absent on some records must be optional "
            "(select(css, optional=True)); do NOT use a Python dict literal."
        )
    if state.offer_sibling:
        parts.append(
            "If the MISSING records are not on THIS page but on a SEPARATE sibling page (e.g. this is "
            "the PAST/archived page and the UPCOMING records are at a different URL, or vice versa), "
            "reply with EXACTLY `SIBLING: <that full url>` (nothing else) and it will be extracted "
            "separately and combined. Only when the records are genuinely on another page, not merely "
            "a section lower on THIS one."
        )
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
    (only when offered) schedules a split. The raw reply is emitted before parse."""
    opening = _opening(state)
    follow = _follow_up(state) if (state.opened or state.check_note or state.last_error) else ""
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
    try:
        state.query = reroot(
            parse_query(reply), state.reference.url, profile=state.reference.profile or None
        )
        state.last_error = state.hint = ""
    except QueryError as exc:  # unparseable / disallowed -> a repair turn re-authors with this
        state.last_error = f"the reply was not a valid wq query ({exc})"
        state.hint = (
            "Reply with ONLY query code -- one wq.doc... chain (or, for a split dataset, one chain "
            "per section separated by a line containing only ---). Nothing else."
        )


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
    except asyncio.TimeoutError:
        state.rows, state.rows_full = [], []
        state.last_error = f"the query test exceeded {_QUERY_TEST_TIMEOUT:.0f}s and was cancelled"
        state.hint = "Avoid a per-record .resolve() over many rows unless a field truly needs it."
    good = _populated(state.rows)
    missing = _missing(state.brief, good) if good else []
    link = _record_link(good) if (missing and not state.nested) else None
    if not state.last_error and good and not missing and state.review is not None:
        ok, note = await _review_rows(state)  # a real, complete sample -> REVIEW it
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
        state.checked = False
        state.check_note = state.last_error = state.hint = ""
        state.offer_sibling = state.nested = False
        state.detail_skeleton = ""
        state.repairs = 0
        state.prev_missing = set()
    elif turn == "check":
        sample = await state.resolver.resolve(state.reference.url)
        state.listing_skeleton = skeleton_for(sample)  # ONE clipped skeleton, reused by every turn
        state.reference = state.reference.model_copy(
            update={"kind": sample.kind, "assessment": list(flags(sample))}
        )
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
        detail = await state.resolver.resolve(link)
        state.detail_skeleton = skeleton_for(detail)
        state.nested = True
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
    parts = [
        *state.sections,
        *([(state.query, list(state.rows_full))] if state.query is not None else []),
    ]
    all_rows: list[object] = [r for _, rows in parts for r in _populated(rows)]
    json_rows = _json_rows(all_rows)
    tested = bool(parts) and all(len(rows) > 0 for _, rows in parts)
    missing = _missing(state.brief, all_rows) if all_rows else list(state.brief.fields)
    tnote, stale = timeliness(json_rows, state.brief)
    primary = parts[0][0] if parts else None
    reason = verdict.reason + (f": {verdict.error}" if verdict.error else "")
    return QueryArtifact(
        blob=primary.to_blob() if primary is not None else "",
        describe=primary.describe() if primary is not None else "",
        tested=tested,
        complete=bool(tested and all_rows and not [m for m in missing if m not in state.absent]),
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
        absent=sorted(state.absent),
        timeliness=tnote,
        stale=stale,
        reason=reason,
    )


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
) -> "tuple[AuthorState, Verdict]":
    state = AuthorState(
        reference=reference,
        brief=brief,
        resolver=_fetch_resolver(reference, resolver.pool),
        llm=llm,
        review=review,
        entity=entity,
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
    try:
        verdict = await asyncio.wait_for(loop.arun(state), timeout=budget_s)
    except asyncio.TimeoutError:  # a slow model / stuck page -- take the sections so far
        emit(
            ReasonEvent(stage="author", text=f"authoring hit the {budget_s:.0f}s budget — stopping")
        )
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
    budget_s: float = 240.0,
) -> QueryArtifact:
    """Author the query for ``reference`` per ``brief`` and return the :class:`QueryArtifact`: the
    runnable query + its validation verdict (see the module docstring). ``review`` enables the
    entry check + per-sample review; ``budget_s`` is a hard wall clock for the whole loop."""
    state, verdict = await _run(
        reference,
        brief,
        resolver=resolver,
        llm=llm,
        review=review,
        entity=entity,
        max_rounds=max_rounds,
        budget_s=budget_s,
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
    budget_s: float = 240.0,
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
    )
    queries = [q for q, _ in state.sections]
    if state.query is not None:
        queries.append(state.query)
    return queries, verdict


__all__ = ["author_agent", "write_query", "AuthorState"]
