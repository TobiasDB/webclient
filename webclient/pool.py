"""Bounded leases over the concrete clients.

Transports are the pool's business, not the WebClient's: an http lease hands
out a reusable `httpx.AsyncClient`, a page lease hands out a playwright Page
from the session's context. Both are created by factories the pool owns, so
retries, proxy selection and lifetime live in one place.
"""
from __future__ import annotations

import asyncio
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel

LeaseKind = Literal["http", "page"]


class Lease:
    """A held resource. One lease is one unit of parallelism."""

    __slots__ = ("id", "kind", "session_id", "client", "page", "_pool")

    def __init__(self, kind: LeaseKind, pool: "ClientPool",
                 session_id: str | None = None) -> None:
        self.id = uuid4().hex
        self.kind = kind
        self.session_id = session_id
        self.client: Any = None
        self.page: Any = None
        self._pool = pool

    async def release(self) -> None:
        await self._pool.release(self)

    def __repr__(self) -> str:
        return f"Lease({self.kind}, {self.id[:8]})"


class PoolStats(BaseModel):
    http_total: int = 0
    http_free: int = 0
    http_held: int = 0
    pages_total: int = 0
    pages_held: int = 0
    waiting: int = 0


class ClientPool:
    """Bounded, FIFO, and honest about what is outstanding."""

    def __init__(self, *, max_http: int = 10, max_pages: int = 4,
                 acquire_timeout: float = 60.0) -> None:
        self.max_http = max_http
        self.max_pages = max_pages
        self.acquire_timeout = acquire_timeout
        self.owner: Any = None

        self._http_semaphore: asyncio.Semaphore | None = None
        self._page_semaphore: asyncio.Semaphore | None = None
        self._idle: list[Any] = []
        self._held: dict[str, Any] = {}
        self._pages: dict[str, Any] = {}
        self._http_created = 0
        self._pages_created = 0
        self._proxy_index = 0
        self._waiting = 0

    # -- acquisition ---------------------------------------------------------
    async def acquire(self, kind: LeaseKind, *, session: Any = None,
                      timeout: float | None = None) -> Lease:
        if kind == "page":
            return await self._acquire_page(session, timeout)
        return await self._acquire_http(session, timeout)

    async def _gate(self, semaphore: asyncio.Semaphore, timeout: float | None,
                    what: str) -> None:
        self._waiting += 1
        try:
            await asyncio.wait_for(
                semaphore.acquire(),
                timeout if timeout is not None else self.acquire_timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"pool exhausted: no {what} lease within "
                f"{timeout or self.acquire_timeout}s") from None
        finally:
            self._waiting -= 1

    async def _acquire_http(self, session: Any, timeout: float | None) -> Lease:
        if self._http_semaphore is None:
            self._http_semaphore = asyncio.Semaphore(self.max_http)
        await self._gate(self._http_semaphore, timeout, "http")
        lease = Lease("http", self, getattr(session, "id", None))
        lease.client = self._idle.pop() if self._idle else self._new_http(session)
        self._held[lease.id] = lease.client
        return lease

    async def _acquire_page(self, session: Any, timeout: float | None) -> Lease:
        if self._page_semaphore is None:
            self._page_semaphore = asyncio.Semaphore(self.max_pages)
        await self._gate(self._page_semaphore, timeout, "page")
        try:
            page = await self.owner._browser().new_page(session)
        except BaseException:
            self._page_semaphore.release()
            raise
        self._pages_created += 1
        lease = Lease("page", self, getattr(session, "id", None))
        lease.page = page
        self._pages[lease.id] = page
        return lease

    # -- release -------------------------------------------------------------
    async def release(self, lease: Lease) -> None:
        if lease.kind == "page":
            page = self._pages.pop(lease.id, None)
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass
                if self._page_semaphore is not None:
                    self._page_semaphore.release()
            return
        client = self._held.pop(lease.id, None)
        if client is not None:
            client.cookies.clear()      # Session owns cookie state, not the
            self._idle.append(client)   # borrowed transport
            if self._http_semaphore is not None:
                self._http_semaphore.release()

    # -- factories -----------------------------------------------------------
    def _new_http(self, session: Any) -> Any:
        import httpx
        owner = self.owner
        proxy_url: str | None = None
        proxy = getattr(session, "proxy", None)
        if proxy is None and owner is not None and owner.proxy_pool:
            proxy = owner.proxy_pool[self._proxy_index % len(owner.proxy_pool)]
            self._proxy_index += 1
        if proxy is not None:
            proxy_url = proxy.authenticated_url
        self._http_created += 1
        return httpx.AsyncClient(
            verify=owner.verify_tls if owner is not None else True,
            proxy=proxy_url, follow_redirects=False)

    # -- introspection -------------------------------------------------------
    def stats(self) -> PoolStats:
        return PoolStats(
            http_total=self._http_created, http_free=len(self._idle),
            http_held=len(self._held), pages_total=self._pages_created,
            pages_held=len(self._pages), waiting=self._waiting)

    async def aclose(self) -> None:
        for page in list(self._pages.values()):
            try:
                await page.close()
            except Exception:
                pass
        self._pages.clear()
        clients = [*self._idle, *self._held.values()]
        self._idle.clear()
        self._held.clear()
        for client in clients:
            try:
                await client.aclose()
            except Exception:
                pass
