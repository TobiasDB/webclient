"""Stage 2 -- review search: ONE model call over the scored hits (one line each) returns the ones
worth following, each as ``must`` (the listing itself), ``could`` or ``lead``."""

from __future__ import annotations

from pydantic import BaseModel

from ..ask import PROMPT_INPUT_CHARS, Context, ask_json
from ..state import Onboarding, Pick, SearchReview


class _Pick(BaseModel):
    n: int
    tier: str = "could"
    why: str = ""


class _Reply(BaseModel):
    picks: list[_Pick] = []


def hit_lines(hits: "list[tuple[str, float, str, str]]", budget: int = PROMPT_INPUT_CHARS) -> str:
    """``n. [score] url — title: snippet`` per hit, within the character budget."""
    out: list[str] = []
    used = 0
    for i, (url, score, title, snippet) in enumerate(hits, 1):
        line = f"{i}. [{score:g}] {url}" + (f" — {title[:80]}" if title else "")
        if snippet:
            line += f": {snippet[:100]}"
        if used + len(line) > budget:
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


async def run(state: Onboarding, ctx: Context) -> SearchReview:
    assert state.search is not None
    hits = state.search.hits
    reply = await ask_json(
        ctx,
        state,
        "review_search",
        _Reply,
        goal=state.brief.goal,
        scope=state.brief.scope(),
        results=hit_lines([(h.url, h.score, h.title, h.snippet) for h in hits]),
        note="",
    )
    picks: list[Pick] = []
    for p in reply.picks:
        if not 1 <= p.n <= len(hits):
            continue
        hit = hits[p.n - 1]
        tier = p.tier if p.tier in ("must", "could", "lead") else "could"
        if hit.url not in {q.url for q in picks}:
            picks.append(Pick(url=hit.url, tier=tier, why=p.why, score=hit.score))  # type: ignore[arg-type]
    order = {"must": 0, "could": 1, "lead": 2}
    picks.sort(key=lambda p: (order[p.tier], -p.score))
    dropped = [h.url for h in hits if h.url not in {p.url for p in picks}]
    if not picks:
        state.stopped = "review_search: no result leads to the dataset"
    return SearchReview(picks=picks, dropped=dropped)
