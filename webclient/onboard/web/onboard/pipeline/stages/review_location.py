"""Stage 6 -- review location: one call judging the located source against the brief -- what
expand described (tier, pager, feed, filters) AND the page's full outline (USER: the reviewer
had too little to go on); ``ok`` is advisory (a concern is reported, it never blocks the author)."""

from __future__ import annotations

from pydantic import BaseModel
from web.fetch import emit

from ...llm import ReasonEvent
from ..ask import Context, ask_json
from ..state import DatasetSource, LocationReview, Onboarding
from .expand import page_of
from .review_candidate import skeleton


class _Reply(BaseModel):
    ok: bool = True
    summary: str = ""
    concerns: list[str] = []


def describe(src: DatasetSource) -> str:
    lines = [
        f"url: {src.url} ({src.kind}, tier {src.profile})",
        f"flags: {', '.join(src.flags) or 'none'}",
    ]
    if src.api is not None:
        lines.append(
            f"data api ({src.api.method}"
            + ("" if src.api.usable else ", not replayable -- the page is used")
            + f"): {src.api.url} (records at {src.api.records_path or 'root'}, schema fit {src.api.fit})"
        )
    if src.pagination is not None:
        lines.append(f"pagination: {src.pagination.kind} {src.pagination.next_selector}".rstrip())
    if src.spa is not None:
        lines.append(f"javascript app: {src.spa.reason}")
    for f in src.filters:
        lines.append(f"filters: {f}")
    if src.filtered and not src.filters:
        lines.append("filtered / tabbed sections present")
    title = src.detail.get("title")
    if isinstance(title, str) and title:
        lines.append(f"title: {title[:100]}")
    return "\n".join(lines)


async def run(state: Onboarding, ctx: Context) -> LocationReview:
    assert state.expand is not None
    doc, _snap = await page_of(ctx, state.expand.url, state.expand.profile)
    reply = await ask_json(
        ctx,
        state,
        "review_location",
        _Reply,
        goal=state.brief.goal,
        scope=state.brief.scope(),
        source=describe(state.expand),
        skeleton=skeleton(doc),
        note="",
    )
    emit(
        ReasonEvent(
            stage="review_location",
            text=f"{'ok' if reply.ok else 'CONCERN'} — {reply.summary}"
            + (f" (concerns: {'; '.join(reply.concerns)})" if reply.concerns else ""),
        )
    )
    return LocationReview(ok=reply.ok, summary=reply.summary, concerns=reply.concerns)
