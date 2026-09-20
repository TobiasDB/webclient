"""Fixes for the code-review's low-severity findings: the pool's default-limit consistency
(L1), http.send()'s idempotent-only retry (L3), and one robots.txt load per host under
concurrent crawl edges (L4)."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from webclient.clients.http import HTTPXClient
from webclient.clients.pool import ClientPool
from webclient.core.crawl.backing import CrawlBacking
from webclient.core.reference import from_url


# -- L1: an un-limited kind uses ONE default for the semaphore AND the stats -----------------
def test_pool_default_limit_is_consistent_across_semaphore_and_stats():
    pool = ClientPool({}, limits={})  # no explicit limits -> the default applies uniformly
    cap = ClientPool._DEFAULT_LIMIT
    # a non-recycled kind with no explicit limit: the semaphore cap and the stats total agree
    # (before the fix the semaphore was 10 while _total/_free reported against 0).
    assert pool._semaphore("page")._value == cap
    assert pool._total("page") == cap
    assert pool._free("page") == cap  # nothing held yet


# -- L3: retry a transport error for idempotent methods only, never a POST -------------------
def test_send_retries_idempotent_methods_only(monkeypatch):
    monkeypatch.setattr("webclient.clients.http.asyncio.sleep", _noop_sleep)

    class _FakeHttpx:
        def __init__(self) -> None:
            self.calls = 0
            self.cookies = httpx.Cookies()

        async def request(self, method, url, **kw):
            self.calls += 1
            raise httpx.ConnectError("boom")  # always a transport failure

    async def main() -> None:
        client = HTTPXClient()
        client._httpx = _FakeHttpx()  # type: ignore[assignment]
        # GET is idempotent -> retried (retries=2 -> 3 attempts)
        with pytest.raises(httpx.TransportError):
            await client.send(from_url("http://x/"), headers={}, cookies={}, timeout=1, retries=2)
        assert client._httpx.calls == 3
        # POST is NOT idempotent -> one attempt even with retries=2 (never re-sent)
        client._httpx.calls = 0
        with pytest.raises(httpx.TransportError):
            await client.send(from_url("http://x/", "post"), headers={}, cookies={}, timeout=1, retries=2)
        assert client._httpx.calls == 1

    asyncio.run(main())


async def _noop_sleep(_seconds: float) -> None:
    return None


# -- L4: two concurrent edges on a new host load its robots.txt ONCE -------------------------
def test_robots_txt_loaded_once_under_concurrent_edges():
    loads = {"n": 0}

    class _Backing(CrawlBacking):
        async def _load_robots(self, core, url):  # count + overlap the two racers
            loads["n"] += 1
            await asyncio.sleep(0.05)  # both concurrent calls are inside here at once
            return None  # no robots.txt -> allowed

    backing = _Backing()
    core = SimpleNamespace(_robots={}, _robots_lock=None)  # the two fields _allowed touches

    async def main():
        return await asyncio.gather(
            backing._allowed(core, "http://h/a"),
            backing._allowed(core, "http://h/b"),  # same host, concurrent
        )

    results = asyncio.run(main())
    assert all(results)          # allowed (no robots)
    assert loads["n"] == 1       # the host's robots.txt was fetched ONCE, not once per edge
