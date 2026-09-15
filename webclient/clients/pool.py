"""ClientPool: bounded leases over transport ``Client``s.

    lease(kind) -> ClientFactory.create() -> Client   (bounded by a semaphore)

Each kind (``http`` / ``page``) is capped by a semaphore. http clients are
recycled (reset then returned to an idle list); pages are closed on release.
The pool is the only thing that creates or holds transports; ``WebClient``
just leases.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from .base import Client, ClientFactory


class PoolStats(BaseModel):
    http_total: int = 0
    http_free: int = 0
    pages_total: int = 0
    pages_free: int = 0
    waiting: int = 0


class Lease:
    """A held ``Client``; an async context manager that releases on exit."""

    def __init__(self, pool: "ClientPool", client: Client) -> None:
        self._pool = pool
        self.client = client
        self.kind = client.kind
        self.released = False  # guard against a double-release inflating the permit

    async def __aenter__(self) -> "Lease":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._pool.release(self)


class ClientPool:
    """Bounded, recycling pool of transport clients, one factory per kind."""

    #: kinds whose clients are recycled on release (else closed)
    _RECYCLE = frozenset({"http"})

    def __init__(
        self,
        factories: dict[str, ClientFactory],
        *,
        limits: dict[str, int] | None = None,
        acquire_timeout: float = 60.0,
    ) -> None:
        self._factories = factories
        self._limits = limits or {}
        self.acquire_timeout = acquire_timeout
        self._sem: dict[str, asyncio.Semaphore] = {}
        self._idle: dict[str, list[Client]] = {k: [] for k in factories}
        self._held: dict[str, int] = {k: 0 for k in factories}
        self._created: dict[str, int] = {k: 0 for k in factories}
        self._waiting: dict[str, int] = {k: 0 for k in factories}

    def _semaphore(self, kind: str) -> asyncio.Semaphore:
        if kind not in self._sem:
            self._sem[kind] = asyncio.Semaphore(self._limits.get(kind, 10))
        return self._sem[kind]

    async def lease(self, kind: str) -> Lease:
        sem = self._semaphore(kind)
        self._waiting[kind] += 1
        try:
            await asyncio.wait_for(sem.acquire(), self.acquire_timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"pool exhausted: no {kind} lease within {self.acquire_timeout}s"
            ) from None
        finally:
            self._waiting[kind] -= 1
        try:
            if self._idle[kind]:
                client = self._idle[kind].pop()
            else:
                client = await self._factories[kind].create()
                self._created[kind] += 1
        except BaseException:
            self._semaphore(kind).release()  # don't leak the permit on create failure
            raise
        self._held[kind] += 1
        return Lease(self, client)

    async def release(self, lease: Lease) -> None:
        if lease.released:  # idempotent: a second release must not inflate the permit
            return
        lease.released = True
        kind = lease.kind
        self._held[kind] -= 1
        # always return the permit, even if reset()/recycle/aclose() raises (a
        # crashed browser page whose close() throws, say) -- otherwise the permit
        # bleeds and the pool deadlocks after enough flaky releases.
        try:
            await lease.client.reset()
            if kind in self._RECYCLE:
                self._idle[kind].append(lease.client)
            else:
                await lease.client.aclose()
        finally:
            self._semaphore(kind).release()

    async def aclose(self) -> None:
        for kind, factory in self._factories.items():
            for client in self._idle[kind]:
                await client.aclose()
            self._idle[kind].clear()
            await factory.aclose()

    def _total(self, kind: str) -> int:
        """The denominator ``free`` is measured against. A recycled kind reuses a
        bounded set of warm clients, so its total is how many were ever created
        (<= the limit). A non-recycled kind (pages: closed on release) has no warm
        set -- ``created`` is a cumulative open counter, not a live population -- so
        its total is the concurrency cap (the limit)."""
        if kind in self._RECYCLE:
            return self._created.get(kind, 0)
        return self._limits.get(kind, 0)

    def _free(self, kind: str) -> int:
        """Clients available to lease right now. Recycled kinds hand back idle warm
        clients; non-recycled kinds have none idle (closed on release), so ``free``
        is the unheld capacity (``limit - held``). Both are bounded by ``_total``
        (``held >= 0`` and ``idle`` is a subset of ``created``), so the reported
        ``free`` can never exceed ``total`` -- see ``pages_free``/``pages_total``."""
        if kind in self._RECYCLE:
            return len(self._idle.get(kind, []))
        return self._limits.get(kind, 0) - self._held.get(kind, 0)

    def stats(self) -> PoolStats:
        return PoolStats(
            http_total=self._total("http"),
            http_free=self._free("http"),
            pages_total=self._total("page"),
            pages_free=self._free("page"),
            waiting=sum(self._waiting.values()),
        )


__all__ = ["ClientPool", "Lease", "PoolStats"]
