"""ClientPool lease-accounting regression tests.

A fake page factory (no playwright) exercises the pool's lease/release bookkeeping
deterministically -- pages are the non-recycled kind (closed on release), which is
where the ``pages_free > pages_total`` accounting bug lived.
"""

from __future__ import annotations

import asyncio

import pytest

from webclient.clients import ClientPool, HTTPXFactory
from webclient.clients.base import ClientFactory
from webclient.clients.browser import BrowserClient, PageResult


class _FakePage:
    def __init__(self, url: str) -> None:
        self.url = url


class _FakeBrowserClient(BrowserClient):
    kind = "page"

    def __init__(self) -> None:
        self.page = _FakePage("about:blank")

    async def open(self, url, *, scripts=(), replay=[], wait_stable=True):  # type: ignore[override]
        return PageResult(url, b"<html><body>ok</body></html>", [], [])

    async def reset(self) -> None:
        pass

    async def aclose(self) -> None:
        pass


class _FakeBrowserFactory(ClientFactory):
    kind = "page"

    async def create(self) -> _FakeBrowserClient:
        return _FakeBrowserClient()

    async def aclose(self) -> None:
        pass


def _pool(*, page_limit: int, timeout: float = 8.0) -> ClientPool:
    return ClientPool(
        {"http": HTTPXFactory(), "page": _FakeBrowserFactory()},
        limits={"http": 10, "page": page_limit},
        acquire_timeout=timeout,
    )


def test_stats_free_never_exceeds_total_across_cycles():
    """Regression for the observed `pages_total=3 pages_free=4`: `free` was measured
    against the limit while `total` counted pages-ever-created, so as pages were
    opened and closed `free` sat at the full cap while `total` lagged behind it.
    `free <= total` must hold for both kinds at every point."""

    async def run() -> None:
        pool = _pool(page_limit=4)
        for _ in range(6):  # cycle more times than the cap
            leases = [await pool.lease("page") for _ in range(3)]
            s = pool.stats()
            assert 0 <= s.pages_free <= s.pages_total, s
            assert 0 <= s.http_free <= s.http_total, s
            for lease in leases:
                await pool.release(lease)
            s = pool.stats()
            assert 0 <= s.pages_free <= s.pages_total, s
        await pool.aclose()

    asyncio.run(run())


def test_double_release_is_idempotent():
    async def run() -> None:
        pool = _pool(page_limit=2)
        lease = await pool.lease("page")
        assert pool._held["page"] == 1
        await pool.release(lease)
        await pool.release(lease)  # second release must not inflate the permit
        assert pool._held["page"] == 0
        assert pool.stats().pages_free <= pool.stats().pages_total
        # permit not inflated: still exactly `limit` concurrent leases
        a = await pool.lease("page")
        b = await pool.lease("page")
        assert pool._held["page"] == 2
        pool.acquire_timeout = 0.2
        with pytest.raises(TimeoutError):
            await pool.lease("page")
        await pool.release(a)
        await pool.release(b)
        await pool.aclose()

    asyncio.run(run())


def test_permit_not_leaked_on_create_failure():
    class _BoomFactory(ClientFactory):
        kind = "page"

        async def create(self):  # type: ignore[override]
            raise RuntimeError("launch failed")

        async def aclose(self) -> None:
            pass

    async def run() -> None:
        pool = ClientPool(
            {"page": _BoomFactory()}, limits={"page": 1}, acquire_timeout=0.5
        )
        for _ in range(3):  # a leaked permit would deadlock the 2nd/3rd attempt
            with pytest.raises(RuntimeError):
                await pool.lease("page")
        assert pool._sem["page"]._value == 1  # permit returned every time
        await pool.aclose()

    asyncio.run(run())
