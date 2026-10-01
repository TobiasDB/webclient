"""Stage 9 -- author review: one small call judging the sample rows against the brief. Final
for now; ``next`` is the seam for onboarding a record's own page as a nested source later."""

from __future__ import annotations

import json

from pydantic import BaseModel

from ...prompts import clip
from ..ask import PROMPT_INPUT_CHARS, Context, ask_json
from ..state import AuthorReview, Onboarding


class _Reply(BaseModel):
    ok: bool = False
    notes: str = ""


async def run(state: Onboarding, ctx: Context) -> AuthorReview:
    assert state.author_extract is not None
    ex = state.author_extract
    shown = [  # the pipeline's own columns (_identity / _url) are not the model's concern
        {k: v for k, v in r.items() if not k.startswith("_")} if isinstance(r, dict) else r
        for r in ex.sample[:3]
    ]
    rows = json.dumps(shown, ensure_ascii=False, indent=0, default=str)
    reply = await ask_json(
        ctx,
        state,
        "author_review",
        _Reply,
        goal=state.brief.goal,
        schema=state.brief.schema_lines(),
        count=str(ex.row_count),
        expected=state.brief.expect_rows or "no expectation",
        optional=", ".join(f.name for f in state.brief.fields if f.optional) or "none",
        rows=clip(rows, PROMPT_INPUT_CHARS, "rows", kind="json"),
        note="",
    )
    review = AuthorReview(ok=reply.ok, notes=reply.notes)
    pending = [f for f in state.brief.required if f not in ex.fields or f in ex.misses]
    url_fields = [f.name for f in state.brief.fields if f.type == "url" and f.name in ex.fields]
    if pending and url_fields:  # the rest lives on each record's own page: the nested seam
        review.next, review.detail_field, review.pending = "nested", url_fields[0], pending
    return review
