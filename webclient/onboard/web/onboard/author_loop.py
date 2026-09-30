"""The authoring LOOP strategy -- the iterative counterpart to the one-shot functions in
:mod:`web.onboard.author` (from which it reuses ``AuthorEvent`` / ``sample_skeleton``). It drives
authoring as ``observe -> decide -> apply`` on the agent tier's
:class:`~web.onboard.agent.BoundedLoop` (the same primitive the crawl uses). Each turn EXTENDS the
query toward the brief:

  * **base**   -- author the records on the reference (``select_all(...).extract(...)``),
  * **detail** -- if the brief needs per-record fields that live on a LINKED page (an article's
    body, a product's spec), fetch a sample record's link and re-author the query so those fields
    resolve each record's link and extract from the detail page (the DSL's ref-resolve fan-out),
  * **done**   -- the sampled rows carry every (non-optional) field the brief asked for.

Between turns it SAMPLES (runs the current query) and the observation drives the next turn -- so the
loop takes another turn only when the brief still needs more. Paginate / download are the next turn
types this same loop is built to take. The decider is deterministic (the brief + what's observed);
the model does the AUTHORING inside each turn, over SIGNAL-SELECTED pattern examples
(:func:`~web.onboard.patterns.guide_for`) so the context stays lean. Scripted-LLM testable,
bounded, resumable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from web.fetch import WebException, emit
from web.resolve import Resolver

from .agent import BoundedLoop, Done, Verdict
from .author import AuthorEvent, sample_skeleton
from .compile import Query, QueryError, parse_query, reroot
from .llm import Llm, ReasonEvent
from .models import DatasetBrief, Reference
from .patterns import field_schema, fields_line, guide_for


@dataclass
class AuthorState:
    """What the authoring loop drives: the source + brief, the client, and the query as it grows --
    plus the last sample, the accumulated authoring instructions, and the page skeletons seen."""

    reference: Reference
    brief: DatasetBrief
    resolver: Resolver
    llm: Llm
    #: an optional reviewer LLM (usually the same model): when set, the loop VERIFIES the dataset is
    #: present + on-entity before authoring (a ``check`` turn) and REVIEWS each sample's rows against
    #: the brief + entity (a mismatch/incompleteness is fed into the repair path). ``None`` skips both.
    review: "Llm | None" = None
    entity: str = ""
    query: "Query | None" = None
    rows: "list[object]" = field(default_factory=list)
    instructions: "list[str]" = field(default_factory=list)
    listing_skeleton: str = ""
    detail_skeleton: str = ""
    nested: bool = False
    #: the entry check ran; and any concern it raised. ADVISORY, not fatal -- a skeleton read is
    #: unreliable (haiku rejected pages that in fact extract fine), so the loop still ATTEMPTS and
    #: concludes "no data" only EMPIRICALLY (0 rows after repair). The concern is fed to the author
    #: (e.g. "the data may be JS-loaded" -> a future browser/API escalation).
    checked: bool = False
    check_note: str = ""
    #: the last authoring/run FAILURE (a parse reject, a select-miss, an empty sample, a review
    #: mismatch) -- fed back to the model on a repair turn; cleared once repaired.
    last_error: str = ""
    #: repair turns taken so far, and the cap (a repair edge re-authors with the failure fed back;
    #: bounded so a persistently-broken query cannot loop forever).
    repairs: int = 0
    max_repairs: int = 3


@dataclass
class _Obs:
    phase: str  # "check" | "base" | "extend"
    missing: "list[str]" = field(default_factory=list)
    detail_link: "str | None" = None
    error: str = ""  # a query FAILURE this sample hit (parse/select-miss/empty) -> a repair turn
    rows_empty: bool = False
    can_repair: bool = True  # repair budget not yet spent


def _missing(brief: DatasetBrief, rows: "list[object]") -> "list[str]":
    """The brief's REQUIRED fields not present (or empty) in every sampled row."""
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


async def _check_source(state: AuthorState) -> "tuple[bool, str]":
    """Entry guard: does the located page actually HOLD this dataset (records present, on-entity) --
    the location can be right yet the data absent (an empty section, a login/iframe). ``review`` off
    -> always OK (no model call)."""
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
    ok = _yes(reply)
    emit(ReasonEvent(stage="check", subject=state.reference.url, text=reply.strip()[:200]))
    return ok, reply.strip()[:200]


async def _review_rows(state: AuthorState) -> "tuple[bool, str]":
    """Per-stage review: do the sampled rows really match the brief + entity, with real values and
    the WHOLE dataset (e.g. both upcoming AND archived if the brief asks)? ``review`` off -> OK."""
    if state.review is None or not state.rows:
        return True, ""
    want = state.brief.goal or "the target dataset"
    schema = "\n".join(field_schema(state.brief)) if state.brief.fields else "(the salient fields)"
    sample = json.dumps(state.rows[:5], ensure_ascii=False, default=str)[:1500]
    # brief-SPECIFIC strictness comes from the brief, not the generic prompt -- so an ir-events
    # timeliness rule ("require upcoming, not only archived") never taints, say, a products brief.
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
    ok = _yes(reply)
    emit(ReasonEvent(stage="review", text=reply.strip()[:200]))
    return ok, reply.strip()[:200]


async def _author(state: AuthorState) -> None:
    """Author the query for the current instructions + skeletons (one model call), over the
    signal-selected pattern examples, rooted at the reference. Sets ``state.query`` on success; on a
    parse REJECT it records ``state.last_error`` (for a repair turn) and leaves the query unchanged,
    so an unparseable reply never crashes the loop. The raw reply is emitted before parse."""
    guide = guide_for([], state.reference.kind, detail=bool(state.detail_skeleton))
    parts = [
        guide,
        "----",
        f"Write ONE wq query that extracts this dataset: {state.brief.goal or 'the records'}.",
        fields_line(state.brief),
        "Requirements:\n" + "\n".join("  - " + i for i in state.instructions),
        f"LISTING page skeleton (the records are here):\n{state.listing_skeleton}",
    ]
    if state.brief.author_hint:  # brief author guidance (e.g. suggested patterns for this dataset)
        parts.append(f"Author guidance from the brief: {state.brief.author_hint}")
    if state.brief.review_hint:  # the review criteria are requirements -- the author must know them
        parts.append(f"The extracted data MUST satisfy this requirement: {state.brief.review_hint}")
    if state.check_note:  # the entry review flagged a concern -- surface it, but still attempt
        parts.append(f"NOTE from a reviewer of this page: {state.check_note}")
    if state.detail_skeleton:
        parts.append(
            "A sample DETAIL page (linked from one record) skeleton:\n" + state.detail_skeleton
        )
        parts.append(
            "For a field that is on the DETAIL page, resolve each record's link and select on it, "
            "e.g. body=wq.doc.select('a.headline').attr('href').resolve().select('article').attr('text')."
        )
    if state.last_error:  # a repair turn: show the model its prior query + why it failed
        prior = state.query.describe() if state.query is not None else "(no parseable query yet)"
        parts.append(
            f"Your PREVIOUS query FAILED and must be fixed:\n{prior}\nFAILURE: {state.last_error}\n"
            "Write a CORRECTED wq.doc... chain -- fix the selector that missed / the disallowed "
            "syntax; a field that may be absent on some records must be optional "
            "(select(css, optional=True)); do NOT use a Python dict literal."
        )
    parts.append("Reply with ONLY the wq.doc... chain -- no prose, no code fence.")
    reply = await state.llm.complete("\n\n".join(parts))
    emit(AuthorEvent(phase="reply", reply=reply))
    try:
        state.query = reroot(
            parse_query(reply), state.reference.url, profile=state.reference.profile or None
        )
        state.last_error = ""
    except QueryError as exc:  # unparseable / disallowed -> a repair turn re-authors with this
        state.last_error = f"the reply did not parse as a wq query -- {exc}"
        emit(ReasonEvent(stage="author", text=f"query rejected, will repair: {exc}"))


async def _observe(state: AuthorState) -> _Obs:
    if not state.checked:  # entry guard first (advisory): note whether the data looks present
        return _Obs(phase="check")
    if state.query is None and not state.last_error:
        return _Obs(phase="base")
    if state.last_error:  # the base/repair author just REJECTED the reply -- no query to sample
        return _Obs(
            phase="extend",
            error=state.last_error,
            rows_empty=True,
            can_repair=state.repairs < state.max_repairs,
        )
    try:  # SAMPLE: run the current query
        assert state.query is not None
        result = await state.query.acollect(resolver=state.resolver)
        state.rows = result[:5] if isinstance(result, list) else [result]
    except (
        WebException
    ) as exc:  # a select-miss / transport failure -> feed it back on a repair turn
        state.rows = []
        state.last_error = f"running the query failed -- {exc.error.code}: {exc.error.detail}"
    if state.rows and not state.last_error:  # a real sample -> REVIEW it against the brief + entity
        ok, note = await _review_rows(state)
        if not ok:
            state.last_error = f"the sample does not satisfy the brief: {note}"
    missing = _missing(state.brief, state.rows)
    link = _record_link(state.rows) if (missing and not state.nested) else None
    return _Obs(
        phase="extend",
        missing=missing,
        detail_link=link,
        error=state.last_error,
        rows_empty=not state.rows,
        can_repair=state.repairs < state.max_repairs,
    )


async def _decide(obs: _Obs) -> "str | Done":
    if obs.phase == "check":
        return "check"
    if obs.phase == "base":
        return "base"
    if obs.error or obs.rows_empty:  # a failed/empty/mismatched query -> repair while budget lasts
        return "repair" if obs.can_repair else Done()
    if obs.missing and obs.detail_link is not None:  # fields on a linked detail page -> nest
        return "detail"
    return Done()  # every required field is present (or unreachable) -- stop


async def _apply(state: AuthorState, turn: "str | Done") -> None:
    if turn == "check":
        sample = await state.resolver.resolve(state.reference.url)
        state.listing_skeleton = sample_skeleton(sample)  # reused by the base turn (no re-resolve)
        ok, note = await _check_source(state)
        state.checked = True
        if not ok:  # advisory only -- attempt anyway; a skeleton read is not a reliable veto
            state.check_note = note
            emit(ReasonEvent(stage="check", text=f"concern (advisory, attempting anyway): {note}"))
    elif turn == "base":
        state.instructions = ["extract every listed record with the fields above"]
        emit(ReasonEvent(stage="author", text="authoring the base query for the listed records"))
        await _author(state)
    elif turn == "repair":
        state.repairs += 1
        if not state.last_error:  # an empty sample with no explicit error: the selector missed
            state.last_error = (
                "the query ran but matched 0 records -- the record selector (select_all) is wrong; "
                "pick a different repeating element from the skeleton"
            )
        emit(ReasonEvent(stage="author", text=f"repair {state.repairs}: {state.last_error[:90]}"))
        await _author(state)  # re-authors with last_error shown, then clears it on success
    elif turn == "detail":
        link = _record_link(state.rows)
        if link is None:
            return
        detail = await state.resolver.resolve(link)
        state.detail_skeleton = sample_skeleton(detail)
        state.nested = True
        state.instructions.append(
            "some fields are NOT on the listing -- resolve each record's link and extract them "
            "from the detail page"
        )
        emit(
            ReasonEvent(
                stage="author",
                subject=link,
                text="the brief needs per-record detail -- nesting a detail extraction",
            )
        )
        await _author(state)


async def author_agent(
    reference: Reference,
    brief: DatasetBrief,
    *,
    resolver: Resolver,
    llm: Llm,
    review: "Llm | None" = None,
    entity: str = "",
    max_rounds: int = 8,
) -> "tuple[Query | None, Verdict]":
    """Drive the authoring loop to a query that satisfies the brief, returning the final query + the
    loop :class:`~web.onboard.agent.Verdict`. Turns: ``check`` (an ADVISORY note on whether the data
    looks present + on-entity), ``base``, ``repair`` (re-author with a failure fed back), ``detail``
    (nest a linked-page extraction). ``review`` (usually the same model) enables the entry check +
    per-sample review; ``None`` skips them. The check never vetoes -- a skeleton read is unreliable,
    so absence is concluded EMPIRICALLY (0 rows after repair). The query is ``None`` only if the base
    + repairs never parsed one."""
    state = AuthorState(
        reference=reference, brief=brief, resolver=resolver, llm=llm, review=review, entity=entity
    )
    loop: "BoundedLoop[AuthorState, _Obs, str | Done]" = BoundedLoop(
        observe=_observe,
        decide=_decide,
        apply=_apply,
        done=lambda d: isinstance(d, Done),
        progress=lambda s: s.query.to_blob() if s.query is not None else "",
        max_rounds=max_rounds,
    )
    verdict = await loop.arun(state)
    return state.query, verdict


__all__ = ["author_agent", "AuthorState"]
