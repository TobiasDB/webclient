"""``WebClient``: the context-managed DSL entry -- "the WebClient is just our DSL".

``async with WebClient(profile=...) as wc:`` owns the resolver lifetime and cleanup (no hand-built
fetcher, no ``try/finally``); ``wc.resolve(url)`` / ``wc.ref(url)`` return lazy chains BOUND to that
resolver, so their terminals reuse it. ``wc.crawl(goal)`` reaches many documents. This is the
top-of-stack seam of the session/context-manager direction (fetch + resolve grow the same dual
one-shot/session entry at their own layers).
"""

from __future__ import annotations

from typing import cast

from web.crawl import Crawler, Goal
from web.fetch import ClientPool
from web.parse import Document
from web.resolve import Profile, Resolver

from .expr import Expr
from .plan import Plan, Step
from .surface import LazyDocument, LazyField, LazyReference, _When, wq


class WebClient:
    """A DSL entry that owns a :class:`~web.resolve.Resolver` for the duration of an ``async with``.
    Its lazy chains are bound to that resolver, so a whole session of queries shares one transport
    (cookies, pooled connections, the profile's ladder/politeness)."""

    def __init__(
        self,
        *,
        resolver: "Resolver | None" = None,
        profile: "Profile | None" = None,
        pool: "ClientPool | None" = None,
    ) -> None:
        self._pool = pool or ClientPool()
        self._resolver = resolver or Resolver(profile=profile, pool=self._pool)

    async def __aenter__(self) -> "WebClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self._resolver.aclose()  # closes any sessions it opened
        await self._pool.aclose()  # shuts the shared backends (the browser)

    def ref(self, url: str) -> LazyReference:
        """A lazy reference root bound to this client (``wc.ref(url).resolve()...``)."""
        return cast(LazyReference, Expr(Plan(root="Reference", source=url), self._resolver))

    def resolve(self, url: str) -> LazyDocument:
        """The resolved page as a lazy document chain (``wc.resolve(url).doc()`` /
        ``wc.resolve(url).select_all(...)...``) -- the reference is fetched at the terminal.
        """
        plan = Plan(
            root="Reference",
            source=url,
            steps=[Step(kind="get", name="resolve"), Step(kind="call")],
        )
        return cast(LazyDocument, Expr(plan, self._resolver))

    async def crawl(self, goal: "Goal | str", *, max_pages: int = 50) -> "list[Document]":
        """Reach many documents from a seed / goal, over this client's resolver."""
        g = goal if isinstance(goal, Goal) else Goal(start=goal, max_pages=max_pages)
        return [doc async for doc in Crawler(self._resolver).crawl(g)]

    def when(self, cond: object) -> _When:
        """A conditional column: ``wc.when(cond).then(a).otherwise(b)`` (``.otherwise`` optional -->
        ``None`` else). Same builder as ``wq.when``."""
        return wq.when(cond)

    def field(self, name: str) -> LazyField:
        """A value already extracted in the surrounding row (``wc.field("price") != ""``)."""
        return wq.field(name)


__all__ = ["WebClient"]
