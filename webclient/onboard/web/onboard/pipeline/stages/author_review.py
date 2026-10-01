"""Stage 9 -- author review: the run's mechanical REPORT first (the model sees it too), then one
small call judging the sample rows against the brief. Final for now; ``next`` is the seam for
onboarding a record's own page as a nested source later."""

from __future__ import annotations

import json

from pydantic import BaseModel
from web.fetch import emit

from ...llm import ReasonEvent
from ...prompts import clip
from ..ask import PROMPT_INPUT_CHARS, Context, ask_json
from ..state import AuthorReview, ExtractQuery, Onboarding


class _Reply(BaseModel):
    ok: bool = False
    notes: str = ""


async def run(state: Onboarding, ctx: Context) -> "list[AuthorReview]":
    assert state.author_extract is not None
    return [await _one(state, ctx, ex) for ex in state.author_extract if ex.complete]


async def _one(state: Onboarding, ctx: Context, ex: ExtractQuery) -> AuthorReview:
    shown = [  # the pipeline's own columns (_identity / _url) are not the model's concern
        {k: v for k, v in r.items() if not k.startswith("_")} if isinstance(r, dict) else r
        for r in ex.sample[:3]
    ]
    rows = json.dumps(shown, ensure_ascii=False, indent=0, default=str)
    guide = next((g for g in state.brief.guides() if g.name == ex.name), None)
    fields = state.brief.guide_fields(guide) if guide is not None else state.brief.fields
    reply = await ask_json(
        ctx,
        state,
        "author_review",
        _Reply,
        goal=state.brief.goal
        + (f" — THIS QUERY: {ex.name} ({guide.hint})" if guide and guide.name else ""),
        schema="\n".join(
            f"- {f.name} ({f.type}): {f.description}" + (" [optional]" if f.optional else "")
            for f in fields
        ),
        count=str(ex.row_count),
        expected=state.brief.expect_rows or "no expectation",
        optional=", ".join(f.name for f in fields if f.optional) or "none",
        report=ex.report or "no report",
        rows=clip(rows, PROMPT_INPUT_CHARS, "rows", kind="json"),
        note="",
    )
    review = AuthorReview(name=ex.name, ok=reply.ok, notes=reply.notes)
    emit(
        ReasonEvent(
            stage="author_review",
            text=f"{ex.name + ': ' if ex.name else ''}{'ok' if reply.ok else 'REJECTED'} — {reply.notes}",
        )
    )
    required = [f.name for f in fields if not f.optional]
    pending = [f for f in required if f not in ex.fields or f in ex.misses]
    url_fields = [f.name for f in fields if f.type == "url" and f.name in ex.fields]
    if pending and url_fields:  # the rest lives on each record's own page: the nested seam
        review.next, review.detail_field, review.pending = "nested", url_fields[0], pending
    return review
