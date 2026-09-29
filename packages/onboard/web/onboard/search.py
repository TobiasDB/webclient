"""A default :class:`~web.onboard.locate.Search` backend over the ``ddgs`` package (DuckDuckGo and
friends) -- turns a goal / company qualifier into seed URLs when a brief gives no seeds/candidates.

``ddgs`` is an OPTIONAL dependency: it is imported lazily (the one justified inline import -- an
optional-dependency guard) and a clear :class:`~web.fetch.WebException` is raised if it is missing.
Locate stays vendor-agnostic -- this is just the batteries-included default; inject any
``async (goal) -> list[str]`` callable instead.
"""

from __future__ import annotations

import asyncio
import os

from web.fetch import WebException, err


class DdgSearch:
    """A :class:`~web.onboard.locate.Search` backed by ``ddgs``. ``k`` is how many results to seed
    from; ``backend`` names the engine(s) ddgs queries (``"auto"`` falls through duckduckgo/google/
    bing/brave/...; override with ``WEB_SEARCH_BACKEND``). Async so it fits the Search Protocol; the
    sync ``ddgs`` call runs off the event loop in a thread."""

    def __init__(self, *, k: int = 6, backend: "str | None" = None) -> None:
        self._k = k
        self._backend = backend or os.environ.get("WEB_SEARCH_BACKEND", "auto")

    async def __call__(self, goal: str) -> "list[str]":
        return await asyncio.to_thread(self._search, goal)

    def _search(self, goal: str) -> "list[str]":
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
        out: list[str] = []
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict):
                url = row.get("href") or row.get("url")
                if isinstance(url, str) and url and url not in out:
                    out.append(url)
        return out


__all__ = ["DdgSearch"]
