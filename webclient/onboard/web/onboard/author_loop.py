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

import asyncio
import json
from dataclasses import dataclass, field

from web.fetch import ClientPool, WebException, emit
from web.resolve import EscalationPolicy, Resolver
from web.resolve import profiles as _rp

from .agent import BoundedLoop, Done, Verdict
from .author import AuthorEvent, sample_skeleton
from .compile import Query, QueryError, parse_query, reroot
from .llm import Llm, ReasonEvent
from .models import DatasetBrief, Reference
from .patterns import field_schema, fields_line, guide_for


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
    #: SPLIT-SOURCE: a dataset can span separate pages (e.g. an UPCOMING events page and a separate
    #: PAST/archived page). ``sections`` holds the FINISHED per-section queries; when the review says
    #: the sample is incomplete and the missing records live on a sibling page, the model replies
    #: ``SIBLING: <url>`` and the loop finalises the current section, then authors that page as a new
    #: one. The pipeline runs every section and concatenates. ``tried`` guards against re-visiting.
    sections: "list[Query]" = field(default_factory=list)
    sibling: str = ""
    tried: "set[str]" = field(default_factory=set)
    offer_sibling: bool = False  # the current failure is an incompleteness a sibling page might fix
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
    max_repairs: int = 2


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
            "For fields that are on the DETAIL page, write ONE column per field, each following the "
            "SAME record link, e.g. body=wq.doc.select('a.headline').attr('href').resolve()"
            ".select('article').attr('text') and author=...resolve().select('.byline').attr('text'). "
            "Repeating the .attr('href').resolve() per field is correct and cheap -- that page is "
            "fetched ONCE per record and reused for every column (resolves are memoised); do NOT try "
            "to bind one resolved page to many fields."
        )
    if state.last_error:  # a repair turn: show the model its prior query + why it failed
        prior = state.query.describe() if state.query is not None else "(no parseable query yet)"
        parts.append(
            f"Your PREVIOUS query FAILED and must be fixed:\n{prior}\nFAILURE: {state.last_error}\n"
            "Write a CORRECTED wq.doc... chain -- fix the selector that missed / the disallowed "
            "syntax; a field that may be absent on some records must be optional "
            "(select(css, optional=True)); do NOT use a Python dict literal."
        )
    if state.offer_sibling:  # the sample is incomplete -- the rest may live on a SEPARATE page
        parts.append(
            "If the MISSING records are not on THIS page but on a SEPARATE sibling page (e.g. this is "
            "the PAST/archived events page and the UPCOMING events are at a different URL, or vice "
            "versa), reply with EXACTLY `SIBLING: <that full url>` (nothing else) and it will be "
            "extracted separately and combined. Only do this when the records are genuinely on "
            "another page, not merely a section lower on THIS one."
        )
    parts.append("Reply with ONLY the wq.doc... chain (or a single SIBLING: line) -- no prose.")
    reply = await state.llm.complete("\n\n".join(parts))
    emit(AuthorEvent(phase="reply", reply=reply))
    stripped = reply.strip()
    if state.offer_sibling and stripped.upper().startswith("SIBLING:"):
        url = stripped.split(":", 1)[1].strip().split()[0] if ":" in stripped else ""
        if url.startswith(("http://", "https://")) and url not in state.tried:
            state.sibling = url  # the loop will finalise this section and author the sibling page
            state.last_error = ""
            emit(ReasonEvent(stage="author", subject=url, text="a sibling page holds the rest"))
            return
    try:
        state.query = reroot(
            parse_query(reply), state.reference.url, profile=state.reference.profile or None
        )
        state.last_error = ""
    except QueryError as exc:  # unparseable / disallowed -> a repair turn re-authors with this
        state.last_error = f"the reply did not parse as a wq query -- {exc}"
        emit(ReasonEvent(stage="author", text=f"query rejected, will repair: {exc}"))


async def _observe(state: AuthorState) -> _Obs:
    if state.sibling:  # the model named a SEPARATE page for the missing records -> author it too
        return _Obs(phase="split")
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
            state.offer_sibling = True  # incompleteness -> the repair may point to a sibling page
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
    if obs.phase == "split":
        return "split"
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
    if turn == "split":
        # finalise the current section, then re-target the loop at the sibling page as a NEW section
        # (a fresh check -> base there); the pipeline runs every section query and concatenates.
        if state.query is not None:
            state.sections.append(state.query)
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
        state.checked = False
        state.last_error = ""
        state.offer_sibling = False
        state.nested = False
        state.detail_skeleton = ""
        state.repairs = 0  # a fresh repair budget for the new section
    elif turn == "check":
        sample = await state.resolver.resolve(state.reference.url)
        state.listing_skeleton = sample_skeleton(sample)  # reused by the base turn (no re-resolve)
        ok, note = await _check_source(state)
        state.checked = True
        if not ok:  # advisory only -- attempt anyway; a skeleton read is not a reliable veto
            state.check_note = note
            # if the records look absent AND we are on the HTTP tier, the likely cause is a JS-gated
            # page Locate mis-tiered -- the author cannot change transport (that is Locate's call), so
            # say so plainly instead of silently proceeding to a doomed 0-row extraction.
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
    max_rounds: int = 12,
    budget_s: float = 240.0,
) -> "tuple[list[Query], Verdict]":
    """Drive the authoring loop to the SECTION QUERIES that satisfy the brief (usually one; more when
    the dataset spans separate pages), plus the loop :class:`~web.onboard.agent.Verdict`. Turns:
    ``check`` (an ADVISORY note on whether the data looks present + on-entity), ``base``, ``repair``
    (re-author with a failure fed back), ``detail`` (nest a linked-page extraction), ``split`` (the
    sample is incomplete and the rest live on a sibling page -> finalise this section and author that
    page as a new one; the pipeline runs every returned query and concatenates). ``review`` enables
    the entry check + per-sample review; ``None`` skips them. The check never vetoes -- absence is
    concluded EMPIRICALLY (0 rows after repair). Returns ``[]`` only if nothing ever parsed.
    ``budget_s`` is a hard wall-clock cap: the loop chains several model calls, so a slow model or a
    stuck page must not run forever -- on the cap we return the sections so far with a ``budget``
    verdict rather than hang."""
    # the author FETCHES with the transport Locate baked, not the pipeline's escalating resolver --
    # so it writes selectors over the SAME content the query samples, and never does transport itself.
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
    except (
        asyncio.TimeoutError
    ):  # a slow model / stuck page -- take the sections so far, don't hang
        emit(
            ReasonEvent(stage="author", text=f"authoring hit the {budget_s:.0f}s budget — stopping")
        )
        verdict = Verdict(reason="budget", rounds=state.repairs)
    queries = [q for q in [*state.sections, state.query] if q is not None]
    if not queries and verdict.reason == "error" and verdict.error:
        # a turn raised (an LLM/transport failure, usually a bad key/base_url) -- broadcast WHY so it
        # is not swallowed into a bare "error" reason for a programmatic caller or the -v log.
        emit(ReasonEvent(stage="author", text=f"authoring aborted — {verdict.error}"))
    # broadcast the OUTCOME: the last sample's row count (+ a preview row) so a caller can see whether
    # authoring actually EXTRACTED data -- 0 rows after repairs is a likely miss, not a clean success.
    preview = json.dumps(state.rows[0], ensure_ascii=False, default=str)[:400] if state.rows else ""
    emit(AuthorEvent(phase="done", rows=len(state.rows), sample=preview, reply=state.last_error))
    return queries, verdict


__all__ = ["author_agent", "AuthorState"]
