"""ClientPool: bounded leases over concrete transports.

M2 implements the http side (a reusable set of persistent
``httpx.AsyncClient``s, ISSUES #19 -- a borrowed client is exclusive to its
lease, so plugins may install event hooks for the request's duration).
Page leases land in M4.
"""
from __future__ import annotations

import asyncio
from typing import Any, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, PrivateAttr

LeaseKind = Literal["http", "page"]


class Lease(BaseModel):
    """A held transport resource; one lease == one unit of parallelism."""

    id: str
    kind: LeaseKind
    session_id: str | None = None

    _pool: Any = PrivateAttr(default=None)
    _client: Any = PrivateAttr(default=None)   # httpx.AsyncClient (http kind)
    _page: Any = PrivateAttr(default=None)     # playwright Page (page kind)

    def release(self) -> None:
        if self._pool is not None:
            self._pool.release(self)


class PoolStats(BaseModel):
    http_total: int = 0
    http_free: int = 0
    pages_total: int = 0
    pages_free: int = 0
    waiting: int = 0


class ClientPool(BaseModel):
    max_http: int = 10
    max_pages: int = 4
    acquire_timeout: float = 60.0

    _owner: Any = PrivateAttr(default=None)            # owning WebClient
    _semaphore: Any = PrivateAttr(default=None)        # asyncio.Semaphore
    _idle: list[Any] = PrivateAttr(default_factory=list)
    _held: dict[str, Any] = PrivateAttr(default_factory=dict)
    _created: int = PrivateAttr(default=0)
    _proxy_index: int = PrivateAttr(default=0)
    _waiting: int = PrivateAttr(default=0)
    _page_semaphore: Any = PrivateAttr(default=None)
    _pages_held: dict[str, Any] = PrivateAttr(default_factory=dict)
    _pages_created: int = PrivateAttr(default=0)

    # -- sync facade ---------------------------------------------------------
    def acquire(self, kind: LeaseKind, *, session: Any = None,
                timeout: float | None = None) -> Lease:
        return self._owner._ensure_loop().run(
            self._acquire(kind, session=session, timeout=timeout))

    def release(self, lease: Lease) -> None:
        self._owner._ensure_loop().run(self._release(lease))

    def stats(self) -> PoolStats:
        return PoolStats(
            http_total=self._created,
            http_free=len(self._idle),
            pages_total=self._pages_created,
            pages_free=self.max_pages - len(self._pages_held),
            waiting=self._waiting,
        )

    # -- engine side (runs on the loop) --------------------------------------
    async def _acquire(self, kind: LeaseKind, *, session: Any = None,
                       timeout: float | None = None) -> Lease:
        if kind == "page":
            return await self._acquire_page(session, timeout)
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_http)
        self._waiting += 1
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout if timeout is not None else self.acquire_timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"pool exhausted: no http lease within {self.acquire_timeout}s") from None
        finally:
            self._waiting -= 1
        client = self._idle.pop() if self._idle else self._new_http_client()
        lease = Lease(id=uuid4().hex, kind="http",
                      session_id=getattr(session, "id", None))
        lease._pool = self
        lease._client = client
        self._held[lease.id] = client
        return lease

    async def _release(self, lease: Lease) -> None:
        if lease.kind == "page":
            page = self._pages_held.pop(lease.id, None)
            if page is not None:
                try:
                    await page.close()  # context (session state) stays alive
                except Exception:
                    pass
                self._page_semaphore.release()
            return
        client = self._held.pop(lease.id, None)
        if client is not None:
            client.event_hooks = {}    # a lease returns clean...
            client.cookies.clear()     # ...and must not leak cookies across
            self._idle.append(client)  # sessions (Session owns cookie state)
            self._semaphore.release()

    async def _acquire_page(self, session: Any,
                            timeout: float | None) -> Lease:
        import asyncio as _asyncio
        if self._page_semaphore is None:
            self._page_semaphore = _asyncio.Semaphore(self.max_pages)
        self._waiting += 1
        try:
            await _asyncio.wait_for(
                self._page_semaphore.acquire(),
                timeout if timeout is not None else self.acquire_timeout)
        except _asyncio.TimeoutError:
            raise TimeoutError(
                f"pool exhausted: no page lease within {self.acquire_timeout}s"
            ) from None
        finally:
            self._waiting -= 1
        try:
            page = await self._owner._browser_host().new_page(session)
        except BaseException:
            self._page_semaphore.release()
            raise
        self._pages_created += 1
        lease = Lease(id=uuid4().hex, kind="page",
                      session_id=getattr(session, "id", None))
        lease._pool = self
        lease._page = page
        self._pages_held[lease.id] = page
        return lease

    def _new_http_client(self) -> httpx.AsyncClient:
        owner = self._owner
        proxy_url: str | None = None
        if owner is not None and owner.proxy_pool:
            proxy = owner.proxy_pool[self._proxy_index % len(owner.proxy_pool)]
            self._proxy_index += 1
            proxy_url = proxy.authenticated_url
        self._created += 1
        return httpx.AsyncClient(
            verify=owner.verify_tls if owner is not None else True,
            proxy=proxy_url,
        )

    async def _aclose(self) -> None:
        clients = self._idle + list(self._held.values())
        self._idle.clear()
        self._held.clear()
        for client in clients:
            await client.aclose()
