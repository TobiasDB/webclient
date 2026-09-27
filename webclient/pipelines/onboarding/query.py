"""onboarding.query -- the query-authoring stage: orchestrate authoring a runnable extraction.

The heavy lifting lives in focused siblings -- :mod:`.query_build` (parse + wrap), :mod:`.query_diagnose`
(run, assess, feedback), :mod:`.query_repair` (selector repair), :mod:`.authors` (the engine seam).
This module owns the prompt, the artifact builders, ``run_query`` and the ``write_query`` retry loop;
it re-imports the moved names so ``write_query`` uses them and the package surface (``__init__`` / the
tests that reach ``onboarding.query.<name>``) keeps resolving."""

from typing import Any, Sequence

from ...interface import WebClient, wq
from ...query.expr import from_blob
from ...core.document.models import DatasetHint, PaginationHint
from ...policy import Resolve
from ...llm.guides import lazy_query_guide
from ...llm.prompts import render_prompt
from ...clients.llm import LlmError

from .dates import _timeliness
from .common import LLM, BrowserMode, _fetch, _skeleton_for, log
from .artifacts import Brief, CandidateEval, QueryPart, QueryArtifact
from .llm import _fields_line

# the moved logic this stage drives -- parse/wrap, run/assess/feedback, selector repair, the author seam.
from .query_build import _executable_query, _reroot
from .query_diagnose import (
    _data_rows, _test_query, _populated_rows, _empty_required_fields, _blob_valued_fields, _content_hint,
    _short_fail_reason, _precheck_sections, _split_section_follow_up, _recency_follow_up,
    _should_retry_for_recency, _row_selector, _richer_json_island,
)
from .query_repair import _repair_query
from .query_assess import completeness_note, correctness_note
from .authors import AuthoringError, Author, _make_author


def _query_prompt(brief: Brief, skeleton: str, *, paginated: bool = False, recency: str = "",
                  dataset_summary: str = "") -> str:
    # Deliberately narrow: the packaged query spec + the skeleton + the ask. Nothing
    # about fetching, resolving, or running -- only CSS selectors and the query syntax.
    pager = (
        "\nThe dataset spans multiple pages -- write the query for ONE page exactly as normal; "
        "the pipeline follows the pagination automatically. Do NOT add a 'next' field."
        if paginated else ""
    )
    # what the page IS, from its own signals -- so the model selects the WHOLE current set, not a
    # filtered/archived subset (this is how completeness / correctness / timeliness are met).
    shape = (f"\n\nDATASET SHAPE (from the page's signals -- select the WHOLE current set, not a "
             f"filtered or archived subset):\n{dataset_summary}" if dataset_summary else "")
    hints = (f"\n\nDATASET NOTES (from the brief -- how this dataset is laid out): {brief.hints}"
             if brief.hints else "") + shape
    return render_prompt(
        "write_query",
        guide=lazy_query_guide(),
        description=brief.description,
        fields_line=_fields_line(brief),
        pager=pager,
        skeleton=skeleton,
        hints=hints,
        recency=(f"\n\nRECENCY (from the page evaluation): {recency}" if recency else ""),
    )

def _recency_guidance(ev: "CandidateEval | None") -> str:
    """A short recency instruction for the query writer, from the evaluator's read of the
    sort order + where the most recent records are. Empty for a non-dated dataset."""
    if ev is None:
        return ""
    parts = []
    if ev.sort_order:
        parts.append(f"the records are {ev.sort_order}")
    if ev.recency_hint:
        parts.append(ev.recency_hint)
    if not parts:
        return ""
    return "Prioritise the MOST RECENT records -- " + "; ".join(parts) + "."

def run_query(artifact: QueryArtifact, *, wc: WebClient) -> list[Any]:
    """Run the authored query and concatenate all the rows. Two independent join axes both
    concatenate: every SECTION sub-query (``artifact.parts`` -- an UPCOMING callout + an
    ARCHIVED list on one page) and every base URL (``base_urls`` -- ``/products/cloud`` +
    ``/products/onprem``). A plain single-section query has no ``parts`` and runs via ``blob``.
    Each blob is SELF-CONTAINED (reference + resolve + extraction), so it resolves + extracts
    on its own; each base just re-points the reference."""
    blobs = [p.blob for p in artifact.parts] or [artifact.blob]
    out: list[Any] = []
    for blob in blobs:  # each section sub-query
        base_expr = from_blob(blob, wc)
        for url in artifact.base_urls or []:
            try:
                result = _reroot(base_expr, url).collect()  # self-contained: no context
            except Exception:  # noqa: BLE001 - a base whose query fails contributes nothing
                continue
            out.extend(_data_rows(result))  # extracted data rows, not selected elements
    return out

def _mode_confirms(doc: Any, mode: Any, records: str) -> bool:
    """Walk two pages with ONE pager mode and confirm a genuine, DISTINCT second page exists.
    ``paginate``'s clamp/repeat guard drops a page-2 that re-serves page one; passing ``records``
    (the record selector) makes the repeat check compare RECORD texts, so an IGNORED ``?page=`` /
    ``?offset=`` param, or a Next link that POINTS BACK to page one (a broken/self-referential
    pager, as some sites' first-page '»' does), is recognised as a repeat -> ``len == 1`` -> not
    confirmed. ``len >= 2`` means a working pager."""
    from ...core.document.paginate import pager_kwargs

    try:
        kwargs = pager_kwargs(mode)
        if "click" in kwargs or "scroll" in kwargs:
            return False  # a load-more pager needs a held browser page: not probed, not baked
        if records:
            kwargs["records"] = records  # a repeat is judged by the RECORDS, not a content hash
        return len(list(doc.paginate(**kwargs, max_pages=2))) >= 2
    except Exception:  # noqa: BLE001 - a probe must never break authoring
        return False

def _confirmed_mode(doc: Any, hint: "PaginationHint | None", records: str = "") -> Any:
    """The FIRST detected pager mode (best-first) that actually walks to a distinct second page, or
    ``None`` if none do. Trying every mode -- not just the best -- means a site whose top-ranked pager
    is broken (a '»' Next link that loops to page one) but which ALSO exposes a working ``?page=`` /
    numbered pager still paginates: the broken mode fails the probe and the page-param mode is used.
    ``None`` -> ship page one (mislabelled / clamped / genuinely single page). One fetch per mode tried."""
    if hint is None or not hint.modes:
        # no structured modes -> fall back to the header/rel=next default and confirm it
        return None if not _mode_confirms(doc, None, records) else _DEFAULT_NEXT
    for mode in hint.modes:
        if _mode_confirms(doc, mode, records):
            return mode
    return None

#: the implicit "no hint" pager (rel=next / the Link header), as a sentinel confirmed mode.
_DEFAULT_NEXT = object()

def _artifact_from(
    expr: Any, doc: Any, brief: Brief, candidate_url: str,
    resolve: "Resolve | None", bases: "list[str]", *, paginate: bool = False,
    hint: "PaginationHint | None" = None, dataset: "DatasetHint | None" = None, mode: str = "",
) -> "tuple[QueryArtifact, list[Any]]":
    """Test one authored query against the source and build its :class:`QueryArtifact` (the
    self-contained, runnable blob + validation verdict + the three assessments: timeliness,
    completeness, correctness). Shared by the one-shot and staged authors. The extraction is TESTED
    on the fetched page one only (fast); ``paginate`` bakes a ``.paginate(...)`` into the SHIPPED
    blob (the FIRST pager mode :func:`_confirmed_mode` verifies reaches a distinct second page), so a
    mislabelled / clamped / broken-pager source ships page one instead of paging into nothing.
    ``dataset`` (from ``doc.dataset()``) supplies the shape the completeness/correctness notes read;
    ``mode`` tags the artifact (``"latest"`` / ``"all"`` / ``"single"``). Returns ``(artifact, rows)``."""
    tested, rows = _test_query(expr, doc) if doc.ok else (False, [])
    good = _populated_rows(rows)
    missing = _empty_required_fields(good, brief)  # required leaves empty on every row
    blobs = _blob_valued_fields(good, brief)  # a field grabbed a whole JSON object, not a leaf value
    island = _richer_json_island(expr, doc, len(good))  # the DOM query is a SUBSET of a richer JSON island
    tnote, stale = _timeliness(good, brief)  # over ALL rows; a FLAG, never a ship blocker
    pager_unconfirmed = False
    confirmed = _confirmed_mode(doc, hint, _row_selector(expr) or "") if (paginate and doc.ok) else None
    if paginate and confirmed is None:
        log.info("    pagination probe: no distinct second page -> shipping page one only")
        paginate = False  # don't bake a pager that pages into nothing / a clamp / a loop
        pager_unconfirmed = True  # but pagination WAS expected -- be honest it may be a subset
    compl, covers_all = completeness_note(dataset, paginated=paginate,  # whole dataset? (pagination / filter)
                                          pager_unconfirmed=pager_unconfirmed)
    corr, correct = correctness_note(dataset)  # the right set? (unfiltered, order known)
    pager_mode = None if confirmed is _DEFAULT_NEXT else confirmed  # the CONFIRMED mode (None = rel=next default)
    exe = _executable_query(expr, candidate_url, resolve, paginate=paginate, mode=pager_mode)  # runnable
    try:  # the visual step tree, from the VALID parsed plan (before/independent of testing)
        explain = exe.explain()
    except Exception:  # noqa: BLE001 - never let rendering the explain break authoring
        explain = ""
    art = QueryArtifact(
        blob=exe.to_blob(),
        describe=exe.describe(),
        explain=explain,
        plan=exe._plan.model_dump(mode="json"),
        tested=tested,
        # complete = real rows, every required leaf a VALUE, and NOT a teaser subset of a richer JSON island
        complete=bool(tested and good and not missing and not blobs and island is None),
        row_count=len(good),
        sample=list(good[:5]),
        resolve=(resolve.model_dump(mode="json") if resolve is not None else {}),
        base_urls=bases,
        timeliness=tnote,
        stale=stale,
        mode=mode or ("all" if paginate else "single"),
        completeness=compl,
        covers_all=covers_all,
        correctness=corr,
        correct=correct,
    )
    return art, rows

def _representative_sample(part_rows: "list[list[Any]]", limit: int = 5) -> "list[Any]":
    """A sample that shows EACH non-empty section: one row from every part in turn, then more
    in order, capped at ``limit`` -- so a combined query's sample surfaces both shapes (the
    single upcoming row AND an archived row), not just the first section's rows."""
    out: list[Any] = []
    for row in part_rows:  # first pass: one row from each part, in order
        if row and len(out) < limit:
            out.append(row[0])
    for rows in part_rows:  # second pass: fill from the remainder, in order
        for r in rows[1:]:
            if len(out) >= limit:
                return out
            out.append(r)
    return out

def _combined_artifact(
    exprs: "list[Any]", doc: Any, brief: Brief, candidate_url: str,
    resolve: "Resolve | None", bases: "list[str]", *, dataset: "DatasetHint | None" = None,
) -> "tuple[QueryArtifact, list[Any], list[int]]":
    """Test each SECTION query against the ONE fetched source and build a single combined
    :class:`QueryArtifact` whose rows are the CONCATENATION of every section's rows. Each
    section becomes a self-contained runnable :class:`QueryPart`; the combined verdict
    (``complete`` / ``row_count`` / ``sample`` / ``timeliness``) is judged over the union, and
    the top-level ``blob``/``describe``/``explain`` describe the FIRST section. Returns
    ``(artifact, combined_good_rows, per_section_good_counts)`` -- the counts let the caller
    give per-section feedback on a section that matched nothing."""
    parts: list[QueryPart] = []
    part_goods: list[list[Any]] = []
    all_tested = True
    for expr in exprs:
        tested, rows = _test_query(expr, doc) if doc.ok else (False, [])
        good = _populated_rows(rows)
        all_tested = all_tested and tested
        exe = _executable_query(expr, candidate_url, resolve)  # self-contained + runnable
        try:  # the visual step tree, independent of testing
            explain = exe.explain()
        except Exception:  # noqa: BLE001 - never let rendering the explain break authoring
            explain = ""
        parts.append(QueryPart(blob=exe.to_blob(), describe=exe.describe(),
                               explain=explain, row_count=len(good)))
        part_goods.append(good)
    combined_good = [r for good in part_goods for r in good]
    missing = _empty_required_fields(combined_good, brief)  # required leaves empty across the union
    blobs = _blob_valued_fields(combined_good, brief)  # a field grabbed a whole JSON object, not a leaf
    tnote, stale = _timeliness(combined_good, brief)  # over the union; a FLAG, never a blocker
    compl, covers_all = completeness_note(dataset, paginated=False)  # a split query is not paged
    corr, correct = correctness_note(dataset)
    first = parts[0]
    art = QueryArtifact(
        blob=first.blob,
        describe=first.describe,
        explain=first.explain,
        plan={},  # a combined query has no single plan; each section's plan lives in its blob
        tested=all_tested,
        complete=bool(all_tested and combined_good and not missing and not blobs),  # every leaf a real VALUE
        row_count=len(combined_good),
        sample=_representative_sample(part_goods),
        resolve=(resolve.model_dump(mode="json") if resolve is not None else {}),
        base_urls=bases,
        timeliness=tnote,
        stale=stale,
        mode="all",  # a split (multi-section) query captures the whole set on the page
        completeness=compl,
        covers_all=covers_all,
        correctness=corr,
        correct=correct,
        parts=parts,
    )
    return art, combined_good, [len(g) for g in part_goods]

def write_query(
    candidate_url: str,
    brief: Brief,
    *,
    wc: WebClient,
    llm: LLM,
    browser: BrowserMode = "auto",
    paginated: bool = False,
    pagination_hint: "PaginationHint | None" = None,
    retries: int = 4,
    extra_urls: Sequence[str] = (),
    resolve: "Resolve | None" = None,
    doc: Any = None,
    recency: str = "",
    author_engine: str = "text",
) -> QueryArtifact | None:
    """Have the model author the DOCUMENT-level extraction from the page skeleton, test
    it against the fetched source (``from_blob`` + run -> it must extract DATA rows), and
    return the best :class:`QueryArtifact`. The stored ``blob`` is the SELF-CONTAINED
    executable query -- the extraction wrapped in ``reference(url).resolve(...)`` so it
    runs as is (:func:`_executable_query`). Retries with feedback on an invalid or
    non-extracting query -- the page is sent ONCE and each retry is a short follow-up when the
    model keeps a conversation (see :func:`_author`), else re-sent. ``paginated`` tells the
    author to capture the next-page link; ``extra_urls`` are further base URLs the same query
    also runs against; ``resolve`` bakes the fetch policy (browser tier) into the executable
    query. ``doc`` is the already-fetched source (from the flag-read step) -- reused so we
    don't re-fetch it. ``author_engine`` picks HOW the model authors: ``"text"`` (the default --
    it writes query code) or ``"index"`` (it picks record/field indexes and ``build_query``
    builds the selectors, so it never authors CSS); the test/validation is the same either way."""
    if doc is None:
        doc = _fetch(wc, candidate_url, browser, optional=True)  # escalate auto->browser if blocked
    # a BINARY document (a PDF, an image, a spreadsheet) has nothing to EXTRACT into rows -- the
    # deliverable IS the file. Author a download recipe deterministically (reference + resolve, no
    # model call), so a "download this document" brief still onboards (genericity for odd shapes).
    if doc.ok and getattr(doc, "kind", "html") not in ("html", "xml", "json"):
        exe = _executable_query(wq.doc, candidate_url, resolve)  # reference + resolve, no extraction
        return QueryArtifact(
            blob=exe.to_blob(), describe=f"download the {doc.kind} document ({exe.describe()})",
            plan=exe._plan.model_dump(mode="json"), tested=True, complete=True, row_count=1,
            sample=[{"kind": doc.kind, "url": candidate_url}], mode="single",
            resolve=(resolve.model_dump(mode="json") if resolve is not None else {}),
            base_urls=[candidate_url, *extra_urls],
            completeness="COMPLETENESS: a single binary document -- the whole file.", covers_all=True,
            correctness=f"CORRECTNESS: a {doc.kind} document (not a record set) -- fetched as is.", correct=True,
        )
    skeleton = _skeleton_for(doc) if doc.ok else ""
    dataset: "DatasetHint | None" = None  # what the page IS (pagination / filters / order), for the
    try:                                  # prompt (select the whole set) + the completeness/correctness notes.
        dataset = doc.dataset() if doc.ok else None
    except Exception:  # noqa: BLE001 - dataset detection must never break authoring
        dataset = None
    prompt = _query_prompt(brief, skeleton, paginated=paginated, recency=recency,
                           dataset_summary=dataset.summary if dataset is not None else "")
    bases = [candidate_url, *extra_urls]
    best: QueryArtifact | None = None
    best_complete: QueryArtifact | None = None  # a complete-but-STALE fallback (recency retries)
    attempts: list[str] = []  # why each rejected attempt was rejected, for the onboard output
    nudged_empty = False  # a split query with an empty section is nudged ONCE, then accepted
    nudged_recency = False  # a stale-but-complete query is pushed for fresher data ONCE, then kept
    prev_missing: set[str] = set()  # required field(s) left empty by the PREVIOUS attempt (records matched)
    absent: set[str] = set()  # required field(s) found to be genuinely absent from the source (early-stop)
    author: Author = _make_author(llm, prompt, doc, brief, engine=author_engine)  # text | index (build_query)
    follow_up: "str | None" = None
    tries = retries + 1

    def _finalize(art: QueryArtifact, expr: Any = None) -> QueryArtifact:
        art.attempts = list(attempts)  # attach the rejection trail to the query we return
        # the A companion: when the shipped query walks pages (mode "all"), also author the LATEST
        # (page one, no backfill) from the same extraction -- a cheap incremental poll of the newest
        # rows. Only for a paginated single-section source (latest == all otherwise).
        if expr is not None and art.mode == "all" and not art.parts and doc.ok:
            try:
                latest, _ = _artifact_from(expr, doc, brief, candidate_url, resolve, bases,
                                           paginate=False, dataset=dataset, mode="latest")
                art.latest = latest
            except Exception:  # noqa: BLE001 - the companion is best-effort, never a blocker
                pass
        return art
    for attempt in range(tries):
        tag = f"    query {attempt + 1}/{tries}"
        try:
            exprs = author.author(follow_up)  # the authoring seam: 1..N query exprs (Phase 6 swaps it)
        except LlmError as exc:  # a bad-request / exhausted-retry API error
            log.warning("%s: LLM call failed (%s)", tag, exc)
            break
        except AuthoringError as exc:  # unparsable/unusable query -> retry with feedback
            log.info("%s: reply was not a valid query (%s) — retrying", tag, exc)
            attempts.append(f"attempt {attempt + 1}: not a valid query ({exc})")
            follow_up = ("Your previous reply was not a valid query. Reply with ONLY query code --"
                         " one wq.doc... chain, or, for a split dataset, one chain PER section"
                         " separated by a line containing only ---. Nothing else.")
            continue
        # every SECTION must select its records, and none may resolve() a value -- reject before
        # it looks like a 0-row "success" (a targeted, per-section reason on a multi-part reply).
        pre = _precheck_sections(exprs)
        if pre is not None:
            reason, follow_up = pre
            log.info("%s: %s — retrying", tag, reason)
            attempts.append(f"attempt {attempt + 1}: {reason}")
            continue
        if len(exprs) > 1:  # a SPLIT dataset: test each section, concatenate, judge the UNION
            art, rows, counts = _combined_artifact(exprs, doc, brief, candidate_url, resolve, bases, dataset=dataset)
            empties = [i + 1 for i, c in enumerate(counts) if c == 0]
            if art.complete:
                if _should_retry_for_recency(art, attempt, tries, nudged_recency):  # stale union -> push for recent (ONCE)
                    nudged_recency, best_complete = True, art
                    attempts.append(f"attempt {attempt + 1}: split query complete but stale — {art.timeliness}")
                    log.info("%s: split query complete but STALE — retrying (%s)", tag, art.timeliness)
                    follow_up = _recency_follow_up(art)
                    continue
                if empties and not nudged_empty:  # working query in hand; nudge ONCE to fill the empty section
                    nudged_empty, best_complete = True, art
                    which = ", ".join(map(str, empties))
                    attempts.append(f"attempt {attempt + 1}: split query section(s) {which} matched 0 records")
                    log.info("%s: split query section(s) %s empty — nudging once", tag, which)
                    follow_up = _split_section_follow_up(empties, exprs)
                    continue
                note = f" (STALE: {art.timeliness})" if art.stale else ""
                log.info("%s: ✓ complete split query%s — %d row(s) across %d section(s)",
                         tag, note, art.row_count, len(exprs))
                return _finalize(art)
            best = best or art  # keep the first rebuildable split as a fallback (NOT complete)
            reason = (f"split query section(s) {', '.join(map(str, empties))} matched 0 records"
                      if empties else f"split query {_short_fail_reason(exprs[0], rows, brief, doc)}")
            attempts.append(f"attempt {attempt + 1}: {reason}")
            log.info("%s: %s — retrying", tag, reason)
            follow_up = _split_section_follow_up(empties, exprs)
            continue
        expr = exprs[0]
        art, rows = _artifact_from(expr, doc, brief, candidate_url, resolve, bases, paginate=paginated, hint=pagination_hint, dataset=dataset)
        if art.complete:
            if not _should_retry_for_recency(art, attempt, tries, nudged_recency):
                note = f" (STALE flag: {art.timeliness})" if art.stale else ""
                log.info("%s: ✓ complete%s — %d row(s)", tag, note, art.row_count)
                return _finalize(art, expr)
            nudged_recency, best_complete = True, art  # complete but stale: push for fresher data ONCE, else keep
            attempts.append(f"attempt {attempt + 1}: complete but stale — {art.timeliness}")
            log.info("%s: complete but STALE — retrying for the most recent data (%s)",
                     tag, art.timeliness)
            follow_up = _recency_follow_up(art)
            continue
        # AUTO-REPAIR a near-miss field selector (a one-char class typo: widget vs widgets) by
        # swapping in the nearest real class present in the record, then re-validate.
        repaired = _repair_query(expr, doc)
        if repaired is not None:
            rart, _rrows = _artifact_from(repaired, doc, brief, candidate_url, resolve, bases, paginate=paginated, hint=pagination_hint, dataset=dataset)
            if rart.complete and not _should_retry_for_recency(rart, attempt, tries, nudged_recency):
                note = f" (STALE flag: {rart.timeliness})" if rart.stale else ""
                log.info("%s: ✓ complete after auto-repairing a selector%s — %d row(s)",
                         tag, note, rart.row_count)
                return _finalize(rart, repaired)
            if rart.complete and rart.stale and not nudged_recency:  # repaired + stale -> push recency ONCE
                nudged_recency, best_complete = True, rart
                attempts.append(f"attempt {attempt + 1}: repaired + complete but stale — {rart.timeliness}")
                log.info("%s: repaired + complete but STALE — retrying for recent (%s)",
                         tag, rart.timeliness)
                follow_up = _recency_follow_up(rart)
                continue
            art = rart if rart.row_count > art.row_count else art  # keep the better fallback
        # a required field the model CANNOT populate is either a wrong selector or a field that is
        # genuinely not on the page. We can't know on the first miss, so we retry with a targeted
        # hint; but if the SAME required field(s) stay empty on the NEXT attempt (records matched and
        # the other fields came out), the field is absent from the source -- stop re-authoring it and
        # keep the best partial, rather than burning every retry on a field that isn't there.
        good = _populated_rows(rows)
        missing = set(_empty_required_fields(good, brief)) if good else set()
        if good and missing and missing <= prev_missing:
            absent |= missing
            best = art if (best is None or art.row_count > best.row_count) else best
            attempts.append(f"attempt {attempt + 1}: field(s) {', '.join(sorted(missing))} absent from the source")
            log.info("%s: field(s) %s absent from the source (records + other fields extract cleanly) "
                     "— keeping the partial, no more retries", tag, ", ".join(sorted(missing)))
            break
        prev_missing = missing
        best = best or art  # keep the first rebuildable one as a fallback (NOT complete)
        # a concise reason on the console; the full, multi-line diagnostic hint goes to the model.
        reason = _short_fail_reason(expr, rows, brief, doc)
        attempts.append(f"attempt {attempt + 1}: {reason}")
        log.info("%s: %s — retrying", tag, reason)
        follow_up = (
            "That query did not extract the dataset. Produce a MATERIALLY DIFFERENT query --"
            " change the .select_all(...) RECORD selector to a more semantic anchor, don't just"
            f" tweak the fields.\n\nYour previous query was:\n{expr.describe()}\n\n"
            f"{_content_hint(expr, rows, brief, doc)}"
        )
    # a complete-but-stale query (recency retries didn't find fresher data) beats an incomplete
    # one: it's a working query, and staleness is a FLAG for the human review, not a blocker.
    if best_complete is not None:
        log.info("    keeping the complete query with a STALE flag (%s)", best_complete.timeliness)
        return _finalize(best_complete)
    if best is None:  # every attempt failed to author a usable query -- say so loudly
        log.warning("    could not author a working query in %d attempt(s)", tries)
        return None
    if absent:  # record the genuinely-absent required field(s) on the partial we return
        best.absent = sorted(absent)
    return _finalize(best)
