"""Search -- the FIRST stage: seed URLs for an entity + brief.

Seeds come from WEB SEARCH -- real result URLs (``ddgs``: DuckDuckGo and friends), NEVER a URL the
model guessed (a guessed path hallucinates, 404s, and looks like "the right page" before it dies).
The query is DETERMINISTIC from the brief (``"<entity> <brief.search>"``, folded by
:meth:`Brief.with_entity`). When a model is available it then VERIFIES each result really belongs
to the entity -- judging from the domain + title + snippet -- dropping look-alike organisations and
aggregators. That filter FAILS OPEN (no usable judgement -> keep everything), and the search
retries a stricter then a broader query when the first result set is empty or all off-entity.
What the model sees here is a short listing of ``url [title] snippet`` lines, clipped to a budget.

``ddgs`` is an OPTIONAL dependency: imported lazily (the one justified inline import -- an
optional-dependency guard) with a clear :class:`~web.fetch.WebException` if it is missing. Locate
stays vendor-agnostic: inject any :class:`Search` callable instead.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from web.fetch import WebException, emit, err

from .llm import Llm, ReasonEvent, parse_json
from .models import LocateBrief, SearchHit
from .patterns import brief_hints
from .prompts import MAX_LISTING_CHARS, clip, render_prompt


@runtime_checkable
class Search(Protocol):
    """Turn a query into seed results. Injected (a search API / a fixed list / a test stub) so
    Locate stays transport-agnostic and offline-testable. Bare URLs are accepted too (a stub);
    :class:`SearchHit`s carry the title + snippet the verify filter judges by."""

    async def __call__(self, goal: str) -> "Sequence[str | SearchHit]": ...


class DdgSearch:
    """A :class:`Search` backed by ``ddgs``. ``k`` is how many results to seed from; ``backend`` names
    the engine(s) ddgs queries (``"auto"`` falls through duckduckgo/google/bing/brave/...; override
    with ``WEB_SEARCH_BACKEND``). Async so it fits the Protocol; the sync ``ddgs`` call runs off the
    event loop in a thread."""

    def __init__(self, *, k: int = 10, backend: "str | None" = None) -> None:
        self._k = k
        self._backend = backend or os.environ.get("WEB_SEARCH_BACKEND", "auto")

    async def __call__(self, goal: str) -> "list[SearchHit]":
        return await asyncio.to_thread(self._search, goal)

    def _search(self, goal: str) -> "list[SearchHit]":
        try:
            from ddgs import DDGS  # type: ignore[import-not-found, unused-ignore]  # optional dep
        except ImportError as exc:
            raise WebException(
                err(
                    "search.no_backend",
                    "web search needs the 'ddgs' package (uv pip install ddgs), or pass your own "
                    "search=... callable to locate()",
                )
            ) from exc
        try:
            with DDGS() as ddgs:
                rows: object = ddgs.text(goal, max_results=self._k, backend=self._backend) or []
        except Exception as exc:  # ddgs raises on rate limits / no results / timeouts
            raise WebException(err("search.failed", str(exc), query=goal)) from exc
        out: list[SearchHit] = []
        seen: set[str] = set()
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            url = row.get("href") or row.get("url")
            if isinstance(url, str) and url and url not in seen:
                seen.add(url)
                out.append(
                    SearchHit(
                        url=url,
                        title=str(row.get("title") or ""),
                        snippet=str(row.get("body") or row.get("snippet") or ""),
                    )
                )
        return out


def as_hits(results: "Sequence[str | SearchHit]") -> "list[SearchHit]":
    """Normalise a search's results to :class:`SearchHit`s (a bare URL becomes an untitled hit)."""
    out: list[SearchHit] = []
    for r in results:
        hit = r if isinstance(r, SearchHit) else SearchHit(url=r)
        if hit.url and hit.url not in {h.url for h in out}:
            out.append(hit)
    return out


def search_query(brief: LocateBrief, entity: str, *, mode: str = "normal") -> str:
    """The web-search query -- DETERMINISTIC from the brief's ``search`` qualifier (already folded
    with the entity by :meth:`Brief.with_entity`), else the goal. ``mode`` steers a retry:
    ``"disambiguate"`` adds a distinguishing hint (a look-alike came back), ``"broaden"`` drops to
    the bare entity plus one site-type hint (nothing came back)."""
    hint = (brief.look[0] if brief.look else "") or brief.title
    if mode == "broaden":
        return f"{entity} {hint}".strip() or entity.strip() or brief.goal
    query = brief.search or brief.goal
    if mode == "disambiguate":
        return f"{query} {hint}".strip()
    return query


async def verify_seeds(
    hits: "list[SearchHit]", entity: str, brief: LocateBrief, llm: "Llm | None"
) -> "list[SearchHit]":
    """Keep only the results that actually belong to ``entity``: the model rejects look-alike
    organisations with a similar name, unrelated orgs and aggregators, judging from the domain +
    title + snippet. What it sees: a short numbered listing, clipped. FAILS OPEN -- no model, no
    entity, or no usable judgement -> every hit is kept (never silently drop everything)."""
    if llm is None or not entity or not hits:
        return list(hits)
    listing = "\n".join(f"{i}. {h.url}  [{h.title}]  {h.snippet}"[:300] for i, h in enumerate(hits))
    prompt = render_prompt(
        "verify_seeds",
        entity=entity,
        description=brief.goal or "the target dataset",
        fields_line=brief_hints(brief),
        seeds=clip(listing, MAX_LISTING_CHARS, "seed results"),
    )
    try:
        data = parse_json(await llm.complete(prompt))
    except WebException as exc:  # a model outage never sinks the search -- keep the raw hits
        emit(ReasonEvent(stage="search", text=f"seed verification skipped (model error: {exc})"))
        return list(hits)
    belong = data.get("belong") if isinstance(data, dict) else None
    if not isinstance(belong, list):
        return list(hits)  # no usable judgement -> fail open
    keep = {i for i in belong if isinstance(i, int)}
    kept: list[SearchHit] = []
    for i, h in enumerate(hits):
        if i in keep:
            kept.append(h)
        else:
            emit(
                ReasonEvent(
                    stage="search", subject=h.url, text=f"dropped: not {entity}'s ({h.title})"
                )
            )
    return kept


async def search_web(
    brief: LocateBrief, entity: str, *, search: Search, llm: "Llm | None" = None
) -> "list[SearchHit]":
    """Seed results for ``entity`` + ``brief``: search with the deterministic query, VERIFY the
    results belong to the entity (when a model is given), and retry -- STRICTER (a disambiguating
    hint) when results came back but none were the entity's, BROADER (the bare entity) when
    nothing came back at all. Bounded to three attempts. FAIL-OPEN: if verification would drop
    every result on every try, the first raw result set is returned rather than sinking the run
    on a stubborn model judgement."""
    raw: list[SearchHit] = []
    mode = "normal"
    tried: set[str] = set()
    for _attempt in range(3):
        query = search_query(brief, entity, mode=mode)
        if query in tried:  # never re-issue the same query -- force a simpler, different one
            query = f"{entity} {brief.name or brief.title}".strip() or brief.goal
        tried.add(query)
        hits = as_hits(await search(query))
        emit(ReasonEvent(stage="search", text=f"{len(hits)} result(s) for {query!r}"))
        if not hits:  # nothing (or a backend hiccup) -> broaden and retry
            mode = "broaden"
            continue
        raw = raw or hits  # remember the first non-empty set for the fail-open path
        kept = await verify_seeds(hits, entity, brief, llm)
        if kept:
            if len(kept) < len(hits):
                emit(
                    ReasonEvent(
                        stage="search", text=f"{len(kept)}/{len(hits)} result(s) belong to {entity}"
                    )
                )
            return kept
        mode = "disambiguate"  # results, but none the entity's -> a stricter query
        emit(ReasonEvent(stage="search", text=f"no result belongs to {entity} — retrying stricter"))
    if raw:
        emit(
            ReasonEvent(
                stage="search", text="verification dropped every result — using the raw seeds"
            )
        )
    return raw


__all__ = ["Search", "DdgSearch", "as_hits", "search_query", "verify_seeds", "search_web"]
