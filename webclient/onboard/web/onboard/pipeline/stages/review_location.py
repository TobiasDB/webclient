"""Stage 6 -- review location: one small call summarising the located source against the brief;
``ok`` is advisory (a concern is reported, it never blocks the author)."""

from __future__ import annotations

from pydantic import BaseModel
from web.fetch import emit

from ...llm import ReasonEvent
from ..ask import Context, ask_json
from ..state import DatasetSource, LocationReview, Onboarding


class _Reply(BaseModel):
    ok: bool = True
    summary: str = ""
    concerns: list[str] = []


def describe(src: DatasetSource) -> str:
    lines = [
        f"url: {src.url} ({src.kind}, tier {src.profile})",
        f"records detected: {src.records} at {src.record_selector or '(no repeating region)'}",
        f"flags: {', '.join(src.flags) or 'none'}",
    ]
    if src.api is not None:
        lines.append(
            f"data api: {src.api.url} (records at {src.api.records_path or 'root'}, schema fit {src.api.fit})"
        )
    if src.pagination is not None:
        lines.append(f"pagination: {src.pagination.kind} {src.pagination.next_selector}".rstrip())
    if src.spa is not None:
        lines.append(f"javascript app: {src.spa.reason}")
    if src.filtered:
        lines.append("filtered / tabbed sections present")
    title = src.detail.get("title")
    if isinstance(title, str) and title:
        lines.append(f"title: {title[:100]}")
    return "\n".join(lines)


async def run(state: Onboarding, ctx: Context) -> LocationReview:
    assert state.expand is not None
    reply = await ask_json(
        ctx,
        state,
        "review_location",
        _Reply,
        goal=state.brief.goal,
        scope=state.brief.scope(),
        source=describe(state.expand),
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
