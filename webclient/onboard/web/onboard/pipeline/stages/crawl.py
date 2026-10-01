"""Stage 3 -- crawl: from the review's picks, reach the dataset listing. A ``must`` is reviewed
(stage 4) the moment it is reachable -- an accepted one ends the stage. Otherwise the crawl walks
the ``could`` / ``lead`` pages on the entity's own domains: each round the pending links are
scored by the brief's hints, detail-shaped leaves dropped, and the best window reviewed with the
same tiny prompt as stage 2 (at most a few rounds); a fetched page that is itself a listing is
reviewed at once. When no must is accepted the coulds are left for stage 4, best first."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel
from web.crawl import Crawler, FrontierItem, Goal, Select
from web.fetch import emit
from web.parse import Document
from web.resolve import flags

from ...llm import ReasonEvent
from ..ask import Context, ask_json
from ..hints import detail_shaped, registrable
from ..state import CrawlResult, Onboarding, Pick, Visited
from .review_candidate import record_count, review_one
from .review_search import hit_lines
from .search import score_url

#: how many pending links one review sees, and how many model-reviewed rounds a crawl may take.
_WINDOW = 12
_ROUNDS = 3


class _Pick(BaseModel):
    n: int
    tier: str = "could"
    why: str = ""


class _Reply(BaseModel):
    picks: list[_Pick] = []


async def run(state: Onboarding, ctx: Context) -> CrawlResult:
    assert state.review_search is not None
    brief = state.brief
    picks = list(state.review_search.picks)
    result = CrawlResult()
    tiers: dict[str, str] = {p.url: p.tier for p in picks}
    scores: dict[str, float] = {p.url: p.score for p in picks}
    # musts first: evaluated at once -- an accepted one is the location, no crawl needed
    for p in [p for p in picks if p.tier == "must"]:
        review = await review_one(state, ctx, p.url)
        result.reviews.append(review)
        if review.present:
            result.candidates, result.stopped_early = [p], True
            result.note = "a must from the search review holds the dataset"
            return result
    seeds = [p.url for p in picks if p.tier in ("could", "lead")]
    emit(
        ReasonEvent(
            stage="crawl", text=f"crawling from {len(seeds)} seed(s): {', '.join(seeds[:4])}"
        )
    )
    if not seeds:
        result.candidates = []
        state.stopped = "crawl: nothing to crawl (every must was rejected)"
        return result
    domains = {registrable(u) for u in seeds}
    rounds = 0

    async def frontier(pending: "Sequence[FrontierItem]", nxt: Select) -> "Sequence[FrontierItem]":
        nonlocal rounds
        live = [it for it in pending if not detail_shaped(it.url)] or list(pending)
        for it in live:
            if it.url not in scores:
                scores[it.url] = score_url(it.url, brief.search.domain, brief.search.path)[0]
        live.sort(key=lambda it: -scores[it.url])
        window = live[:_WINDOW]
        unknown = [it for it in window if it.url not in tiers]
        if unknown and rounds < _ROUNDS:
            rounds += 1
            reply = await ask_json(
                ctx,
                state,
                "crawl",
                _Reply,
                prompt="review_search",
                goal=brief.goal,
                scope=brief.scope(),
                results=hit_lines([(it.url, scores[it.url], it.text, "") for it in unknown]),
                note="",
            )
            for r in reply.picks:
                if 1 <= r.n <= len(unknown):
                    tier = r.tier if r.tier in ("must", "could", "lead") else "could"
                    tiers[unknown[r.n - 1].url] = tier
                    emit(
                        ReasonEvent(
                            stage="crawl", subject=unknown[r.n - 1].url, text=f"{tier} — {r.why}"
                        )
                    )
            for it in unknown:
                tiers.setdefault(it.url, "drop")
        order = {"must": 0, "could": 1, "lead": 2, "drop": 3}
        keep = [it for it in window if tiers.get(it.url, "lead") != "drop"]
        keep.sort(key=lambda it: (order[tiers.get(it.url, "lead")], -scores[it.url]))
        return keep[:4] if keep else []

    def scope(doc: Document, link: str) -> bool:
        return registrable(link) in domains

    goal = Goal(start=seeds, scope=scope, max_pages=brief.search.max_pages, frontier=(frontier,))
    async for doc in Crawler(ctx.resolver).crawl(goal):
        fired = [f.name for f in flags(doc)]
        count = record_count(doc)
        result.visited.append(
            Visited(
                url=doc.url,
                status=200,
                ok=True,
                kind=doc.kind,
                flags=fired,
                records=count,
                tier=tiers.get(doc.url, ""),
                score=scores.get(doc.url, 0.0),
            )
        )
        emit(
            ReasonEvent(
                stage="crawl",
                subject=doc.url,
                text=f"fetched: {count} record(s), flags {', '.join(fired) or 'none'}, tier "
                f"{tiers.get(doc.url) or '-'}, score {scores.get(doc.url, 0.0):g}",
            )
        )
        listing = (
            "record_list" in fired and not detail_shaped(doc.url) and scores.get(doc.url, 0) > 0
        )
        if tiers.get(doc.url) == "must" or listing:
            review = await review_one(state, ctx, doc.url)
            result.reviews.append(review)
            if review.present:
                result.candidates = [
                    Pick(
                        url=doc.url, tier="must", why=review.reason, score=scores.get(doc.url, 0.0)
                    )
                ]
                result.stopped_early = True
                result.note = "a listing reached by the crawl holds the dataset"
                emit(
                    ReasonEvent(
                        stage="crawl",
                        subject=doc.url,
                        text="the dataset listing — stopping the crawl",
                    )
                )
                return result
    rejected = {r.url for r in result.reviews if not r.present}
    coulds = [
        Pick(url=u, tier="could", why="", score=scores.get(u, 0.0))
        for u, t in tiers.items()
        if t == "could" and u not in rejected
    ]
    coulds.sort(key=lambda p: -p.score)
    result.candidates = coulds
    result.note = f"{len(result.visited)} page(s) crawled, {len(coulds)} could(s) to evaluate"
    emit(ReasonEvent(stage="crawl", text=result.note))
    if not coulds:
        state.stopped = "crawl: no page holds the dataset"
    return result
