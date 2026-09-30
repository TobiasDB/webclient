"""The crawl's frontier MIDDLEWARE -- the turn-based analogue of fetch's request middleware.

Where :mod:`web.fetch.middleware` wraps a ``Request -> Snapshot`` fetch, this wraps the crawl's
per-turn decision ``pending frontier -> the batch to expand``. Each turn the :class:`~web.crawl.Crawler`
asks a :data:`Select` handler which pending :class:`~web.crawl.FrontierItem`\\s to expand next; a
:data:`FrontierMiddleware` is an onion layer around that decision -- it may REORDER (score), PRUNE
(drop off-topic edges), or PICK directly (an LLM choosing the promising links) before, or instead
of, deferring to the next handler. :func:`stack` composes them exactly as ``fetch.stack`` composes
request middleware, and the base handler is :func:`fifo` (breadth-first). So an LLM-driven,
best-first frontier is just a middleware -- the same pattern as fetch -> resolve.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence

from .models import FrontierItem

#: the next step in the chain: given the pending frontier, return the batch to expand THIS turn
#: (a subset -- the returned items are removed from the frontier; ``[]`` stops the crawl).
Select = Callable[[Sequence[FrontierItem]], Awaitable[Sequence[FrontierItem]]]
#: one onion layer: it receives the pending frontier and the next handler, and returns the batch.
FrontierMiddleware = Callable[[Sequence[FrontierItem], Select], Awaitable[Sequence[FrontierItem]]]


async def fifo(pending: "Sequence[FrontierItem]") -> "Sequence[FrontierItem]":
    """The base policy: breadth-first -- expand the oldest pending item (one per turn)."""
    return [pending[0]] if pending else []


def _bind(mw: FrontierMiddleware, nxt: Select) -> Select:
    async def handler(pending: "Sequence[FrontierItem]") -> "Sequence[FrontierItem]":
        return await mw(pending, nxt)

    return handler


def stack(base: Select = fifo, middleware: "tuple[FrontierMiddleware, ...]" = ()) -> Select:
    """Wrap ``base`` in a frontier-middleware chain (outermost first); the result is a Select."""
    handler = base
    for mw in reversed(middleware):  # build the onion inside-out
        handler = _bind(mw, handler)
    return handler


__all__ = ["Select", "FrontierMiddleware", "stack", "fifo"]
