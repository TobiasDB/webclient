"""onboarding.review -- see the package docstring."""


import json
from typing import Any


#: the pipeline's logger. Stages log progress here (seeds, crawl, candidates, the
#: evaluation, the query, spend); a CLI or app sets the level / handler. Each line is
#: also appended to ``OnboardingResult.steps`` for a programmatic trace.

from ...llm.prompts import render_prompt

from .dates import _TIMELINESS_INTERVALS, _CHECK_COMPLETENESS, _COMPLETENESS_BLOCK, _COMPLETENESS_OFF, _parse_date, _DATE_LEAVES, _date_field_paths, _dig, _timeliness  # noqa: F401
from .common import LLM, _MAX_LISTING_CHARS, _MAX_PAGES_CHARS, _skeleton_for, _clip
from .artifacts import Brief, Review, OnboardingResult, _RunArtifacts, _trace
from .llm import _ask_json, _fields_line


# --------------------------------------------------------------------------- #
# 8. review  (LLM meta-review: grade the run's choices)
# --------------------------------------------------------------------------- #


def _as_bool(v: Any) -> bool:
    """Coerce a model's truthy/falsey field to bool, treating the STRING tokens a cheap model
    emits ("false"/"no"/"0"/"") as False -- so ``"pass": "false"`` isn't read as truthy."""
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "no", "0", "", "none", "null")
    return bool(v)


def _review_from_json(stage: str, data: Any) -> "Review | None":
    """Build a :class:`Review` from a model's JSON judgement (``verdict`` / ``score`` /
    ``issues`` / ``summary``). ``None`` if the model gave nothing usable."""
    if not isinstance(data, dict):
        return None
    issues = data.get("issues") or []
    if not isinstance(issues, list):
        issues = [str(issues)]
    try:
        score = int(data.get("score") or 0)
    except (TypeError, ValueError):
        score = 0
    return Review(
        stage=stage,
        verdict=str(data.get("verdict") or ""),
        passed=_as_bool(data.get("pass", True)),  # absent -> default pass; a stringy "false" is False
        score=max(0, min(10, score)),
        issues=[str(i) for i in issues][:10],
        summary=str(data.get("summary") or ""),
    )


def _page_lines(crawl: Any, limit: int = 40) -> str:
    """The crawled pages as ``url [tier] flags — title`` lines (for a review prompt)."""
    out: list[str] = []
    for card in (getattr(crawl, "pages", []) or [])[:limit]:
        tier = getattr(card, "final_tier", "static") or "static"
        flags = ", ".join(getattr(card, "flags", []) or []) or "-"
        title = (getattr(card, "title", "") or "").strip()
        out.append(f"{card.final_url or card.url} [{tier}; {flags}]" + (f" — {title}" if title else ""))
    fails = [f"{f.url} ({f.reason})" for f in (getattr(crawl, "failures", []) or [])[:15]]
    if fails:
        out.append("failed: " + "; ".join(fails))
    return "\n".join(out)


def review_crawl(artifacts: _RunArtifacts, brief: Brief, *, llm: LLM) -> "Review | None":
    """Grade the crawl: did it reach the pages likely to hold the dataset, and was it
    complete (not too shallow, not off down irrelevant paths)?"""
    if artifacts.crawl is None:
        return None
    data = _ask_json(llm, render_prompt(
        "review_crawl",
        description=brief.description, fields_line=_fields_line(brief),
        seeds="\n".join(s.url for s in artifacts.seeds) or "(none)",
        pages=_clip(_page_lines(artifacts.crawl), _MAX_PAGES_CHARS, "crawled pages") or "(none)",
    ))
    return _review_from_json("crawl", data)


def review_select(result: OnboardingResult, artifacts: _RunArtifacts, brief: Brief, *, llm: LLM) -> "Review | None":
    """Grade the selection: from the crawled pages, were the right URLs picked as
    candidates, and was the best source chosen to scrape?"""
    if not artifacts.candidates:
        return None
    chosen = result.evaluation.url if result.evaluation else "(none chosen)"
    cands = "\n".join(f"{c.url} [{c.tier}] {c.note}".rstrip() for c in artifacts.candidates)
    data = _ask_json(llm, render_prompt(
        "review_select",
        description=brief.description, fields_line=_fields_line(brief),
        pages=_clip(_page_lines(artifacts.crawl), _MAX_PAGES_CHARS, "crawled pages") or "(none)",
        candidates=_clip(cands, _MAX_LISTING_CHARS, "candidates"),
        chosen=chosen,
    ))
    return _review_from_json("select", data)


def review_query(result: OnboardingResult, artifacts: _RunArtifacts, brief: Brief, *, llm: LLM) -> "Review | None":
    """Grade the authored query: does the output table hold data matching the brief, are
    the selectors targeting the relevant parts of the page, and do they capture ALL the
    records on the page (completeness)?"""
    q = result.query
    if q is None:
        return None
    doc = artifacts.query_doc
    skeleton = _skeleton_for(doc) if (doc is not None and doc.ok) else "(unavailable)"
    sample = json.dumps(list(q.sample)[:8], default=str, indent=2)
    # timeliness was assessed over ALL rows at authoring time (stored on the artifact); reuse it
    # rather than recomputing from the 5-row sample (which can miss the newest item).
    tnote = q.timeliness or "(no date field to assess timeliness)"
    data = _ask_json(llm, render_prompt(
        "review_query",
        description=brief.description, fields_line=_fields_line(brief),
        query=q.describe, row_count=str(q.row_count), tested=str(q.tested),
        sample=_clip(sample, _MAX_LISTING_CHARS, "sample rows"),
        timeliness=tnote,
        # completeness is KEPT but DISABLED by default -- flip _CHECK_COMPLETENESS to gate on it
        completeness=(_COMPLETENESS_BLOCK if _CHECK_COMPLETENESS else _COMPLETENESS_OFF),
        skeleton=skeleton,
    ))
    review = _review_from_json("query", data)
    if q.stale:  # surface staleness as a FLAG on the review (never abandons the query)
        review = review or Review(stage="query", verdict="partial")
        review.passed = False  # recorded as "flagged" by _note_review, not a hard gate
        if q.timeliness and q.timeliness not in review.issues:
            review.issues = [q.timeliness, *review.issues][:10]
        if not review.summary:
            review.summary = "the most recent data may be missing (timeliness flag)"
    return review


def review_query_exit(result: OnboardingResult, artifacts: _RunArtifacts, brief: Brief, *, llm: LLM) -> "Review | None":
    """Re-check the brief's EXIT CONDITION against the authored query's RESULT (the extracted
    rows), not just the page as the evaluate stage did. When the condition holds on the RESULT
    -- e.g. ir-events' "the upcoming section is empty" reading true because the output has no
    upcoming events -- it is NOTED here, and a genuine clean exit is distinguished from a likely
    MISS (an oddly-formatted single row the record selector skipped). Informational only: it
    never abandons a working query (a query that ran and is complete still ships) -- it ensures
    the brief's own exit semantics are checked against the DATA and surfaced for the human.
    ``None`` when the brief has no exit condition or no query was authored."""
    q = result.query
    if not brief.exit_when or q is None:
        return None
    sample = json.dumps(list(q.sample)[:8], default=str, indent=2)
    data = _ask_json(llm, render_prompt(
        "review_query_exit",
        description=brief.description,
        exit_condition=brief.exit_when,
        row_count=str(q.row_count),
        sample=_clip(sample, _MAX_LISTING_CHARS, "sample rows"),
    ))
    if not isinstance(data, dict):
        return None
    reason = str(data.get("reason") or "")
    if not _as_bool(data.get("met")):  # the condition does NOT hold on the result -> nothing to flag
        return Review(stage="exit", verdict="not met", passed=True,
                      summary=reason or "the brief's exit condition does not hold on the result")
    missed = _as_bool(data.get("likely_missed"))  # holds only because records were probably skipped
    return Review(
        stage="exit",
        verdict="likely miss" if missed else "met",
        passed=not missed,  # a probable miss is FLAGGED; a genuine clean exit is informational
        issues=([f"the query may have MISSED records the brief expects ({brief.exit_when})"]
                if missed else []),
        summary=reason or f"the brief's exit condition holds on the result: {brief.exit_when}",
    )


def review_failure(result: OnboardingResult, artifacts: _RunArtifacts, brief: Brief, *, llm: LLM) -> "Review | None":
    """Diagnose a failed run: from the trace + how far it got, name the most likely cause
    and what would fix it. Included in the summary so a human sees WHY it failed."""
    reached = (
        f"seeds={len(artifacts.seeds)}, crawled={len(getattr(artifacts.crawl, 'pages', []) or [])}, "
        f"candidates={len(artifacts.candidates)}, evaluated={'yes' if result.evaluation else 'no'}, "
        f"query={'yes' if result.query else 'no'}"
        + (f" ({result.query.row_count} rows)" if result.query else "")
    )
    data = _ask_json(llm, render_prompt(
        "review_failure",
        description=brief.description, fields_line=_fields_line(brief),
        reason=result.reason or "(unknown)",
        reached=reached,
        trace="\n".join(result.steps[-25:]) or "(no trace)",
    ))
    return _review_from_json("failure", data)


def _note_review(result: OnboardingResult, review: "Review | None") -> None:
    """Record a stage review as INFORMATION for the human, without abandoning the run. The
    review (an LLM judgement, or the deterministic timeliness assessment) is appended to
    ``result.reviews`` and traced, but a non-passing review NO LONGER fails the pipeline: it is
    a FLAG, not a hard gate. Whether a run ships is decided by the DETERMINISTIC extraction
    result (``q.complete`` -- real rows, every required field populated), not by a model's
    opinion or a timeliness note -- so a useful warning ("the data looks stale", "the crawl
    was thin") is surfaced for review instead of throwing away a working query. ``None`` (the
    review was skipped) records nothing."""
    if review is None:
        return
    result.reviews.append(review)
    _trace(result, "%s review: %s%s", review.stage,
           "passed" if review.passed else "flagged",
           f" — {review.summary}" if review.summary else "")


def diagnose_failure(result: OnboardingResult, artifacts: _RunArtifacts, *, brief: Brief, llm: LLM) -> None:
    """On a failed run (whether a stage review gated it or a stage errored), add the
    failure review's diagnosis to ``result.reviews`` so the summary says WHY it failed."""
    review = review_failure(result, artifacts, brief, llm=llm)
    if review is not None:
        result.reviews.append(review)


