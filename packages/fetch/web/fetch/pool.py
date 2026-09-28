"""``Pool`` -- bound the concurrency of a backend.

A backend can serve many fetches at once (httpx multiplexes; the browser opens a context per
session), but unbounded concurrency exhausts sockets / RAM / the remote host. ``Pool`` wraps a
backend and gates concurrent :meth:`fetch` calls with a semaphore -- so a fan-out crawl keeps at
most ``limit`` in flight. It is itself a :class:`~web.fetch.base.Fetcher`, so it drops in anywhere
a backend does (including as a ladder tier).
"""

from __future__ import annotations

import asyncio

from .base import Fetcher
from .request import Request
from .snapshot import Snapshot


class Pool:
    """A Fetcher that limits ``inner`` to ``limit`` concurrent fetches."""

    def __init__(self, inner: Fetcher, *, limit: int = 4) -> None:
        self._inner = inner
        self._sem = asyncio.Semaphore(limit)
        self.limit = limit
        self.in_flight = 0
        self.peak = 0

    async def fetch(self, request: Request) -> Snapshot:
        async with self._sem:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            try:
                return await self._inner.fetch(request)
            finally:
                self.in_flight -= 1

    async def aclose(self) -> None:
        await self._inner.aclose()


__all__ = ["Pool"]
