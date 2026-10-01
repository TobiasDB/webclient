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
    rows = json.dumps(ex.sample[:3], ensure_ascii=False, indent=0, default=str)
    reply = await ask_json(
        ctx,
        state,
        "author_review",
        _Reply,
        goal=state.brief.goal,
        schema=state.brief.schema_lines(),
        count=str(ex.row_count),
        expected=state.brief.expect_rows or "no expectation",
        rows=clip(rows, PROMPT_INPUT_CHARS, "rows", kind="json"),
        note="",
    )
    return AuthorReview(ok=reply.ok, notes=reply.notes)
