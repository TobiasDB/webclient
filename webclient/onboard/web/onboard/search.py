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

from .models import SearchHit


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


__all__ = ["DdgSearch", "Search", "as_hits"]
