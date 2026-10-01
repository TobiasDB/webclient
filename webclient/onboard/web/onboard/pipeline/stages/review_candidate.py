"""Stage 4 -- review candidate: does the dataset exist on this page? The page's clipped skeleton
and the brief's goal go to the model (one small call). When it says no at the HTTP tier, the page
is rendered in a browser ONCE and asked again; a no at the browser tier rejects the candidate.
Stage 3 calls :func:`review_one` the moment a ``must`` is fetched (early stop); this stage walks
the remaining candidates in order and keeps the first that holds the dataset."""

from __future__ import annotations

import importlib

from pydantic import BaseModel
from web.fetch import Request, Snapshot, emit
from web.parse import Document
from web.resolve import Resolver, document, flags
from web.resolve import profiles as _rp

from ...llm import ReasonEvent
from ...prompts import clip
from ..ask import Context, ask_json
from ..state import CandidateReview, Onboarding

#: this module, looked up at call time so a test's stub of :func:`render` is what runs.
_mod = importlib.import_module(__name__)


class _Reply(BaseModel):
    present: bool = False
    reason: str = ""


#: the page outline a review / the author sees: ~1.4k tokens, shallow enough to cover the WHOLE
#: page (a deep widget -- a PDF viewer's toolbar -- must not eat the budget), chrome dropped, and
#: WITHOUT the record detector's marks (a selector pointer upstream poisons what follows).
SKELETON_CHARS = 5_600
_DEPTH = 12


def skeleton(doc: Document) -> str:
    """The page outline the model judges / authors by: a JSON shape, or the DOM outline with
    chrome dropped, nesting capped, no detector marks, clipped from the top (page order)."""
    if doc.kind == "json":
        return clip(doc.json_skeleton(max_lines=400), SKELETON_CHARS, "skeleton", kind="json")
    outline = doc.skeleton(
        max_lines=600, text_chars=40, max_depth=_DEPTH, mark_records=False, drop_chrome=True
    )
    return clip(outline, SKELETON_CHARS, "skeleton")


#: the author sees the FULL structure (USER 2026-10-01: a small outline made authoring fail);
#: only a safety cap against a pathological page (~25k tokens).
FULL_SKELETON_CHARS = 100_000


def full_skeleton(doc: Document) -> str:
    """The whole document structure for the AUTHOR: full depth and width, chrome dropped, no
    detector marks, clipped only at the safety cap."""
    if doc.kind == "json":
        return clip(doc.json_skeleton(max_lines=4000), FULL_SKELETON_CHARS, "skeleton", kind="json")
    outline = doc.skeleton(
        max_lines=4000, text_chars=60, max_depth=60, mark_records=False, drop_chrome=True
    )
    return clip(outline, FULL_SKELETON_CHARS, "skeleton")


async def _judge(state: Onboarding, ctx: Context, doc: Document, profile: str) -> _Reply:
    return await ask_json(
        ctx,
        state,
        "review_candidate",
        _Reply,
        goal=state.brief.goal,
        scope=state.brief.scope(),
        url=doc.url,
        tier=(
            "a BROWSER (scripts ran)"
            if profile == "full_browser"
            else "the HTTP tier (no script ran: an app's empty shell shows no records here)"
        ),
        skeleton=skeleton(doc),
        note="",
    )


async def render(ctx: Context, url: str) -> Snapshot:
    """The page rendered in a real browser (the top realness tier) -- a module-level seam so a
    test can stub the render instead of launching a browser."""
    return await Resolver(profile=_rp.FULL_BROWSER, pool=ctx.resolver.pool).snapshot(
        Request(url=url)
    )


async def review_one(state: Onboarding, ctx: Context, url: str) -> CandidateReview:
    """Fetch ``url`` (the resolver's tier), judge it; on a no at the HTTP tier render it in a
    browser and judge again. The accepted page's document (and snapshot) stay in ``ctx.docs``."""
    snap = await ctx.resolver.snapshot(Request(url=url))
    doc = document(snap)
    profile = "basic"
    verdict = await _judge(state, ctx, doc, profile)
    retried = False
    if not verdict.present and not any(f.name in ("blocked",) for f in flags(doc, snap)):
        emit(
            ReasonEvent(
                stage="review_candidate",
                subject=url,
                text="not seen at the HTTP tier — rendering in a browser once",
            )
        )
        try:
            snap = await _mod.render(ctx, url)
        except Exception as exc:  # noqa: BLE001 -- no browser available: the HTTP verdict stands
            emit(
                ReasonEvent(stage="review_candidate", subject=url, text=f"no browser render: {exc}")
            )
        else:
            doc, profile, retried = document(snap), "full_browser", True
            verdict = await _judge(state, ctx, doc, profile)
    if verdict.present:
        ctx.docs[url] = (doc, snap)
    emit(
        ReasonEvent(
            stage="review_candidate",
            subject=url,
            text=f"{'present' if verdict.present else 'absent'} at {profile}: {verdict.reason}",
        )
    )
    return CandidateReview(
        url=url,
        present=verdict.present,
        profile=profile,
        reason=verdict.reason,
        retried_browser=retried,
        kind=doc.kind,
    )


async def run(state: Onboarding, ctx: Context) -> CandidateReview:
    assert state.crawl is not None
    done = {r.url: r for r in state.crawl.reviews}
    for pick in state.crawl.candidates:
        review = done.get(pick.url)
        if review is None:
            review = await review_one(state, ctx, pick.url)
            state.crawl.reviews.append(review)
        if review.present:
            return review
    state.stopped = "review_candidate: no candidate holds the dataset"
    return CandidateReview(url="", present=False, reason="no candidate holds the dataset")
