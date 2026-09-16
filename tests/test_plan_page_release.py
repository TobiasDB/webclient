"""Plan-owned browser page release: a plan (or stream) must release each resolved
page as soon as the element finishes, not hold every page until the whole plan
completes -- otherwise a plan resolving more pages than the page-pool cap pins the
cap and deadlocks the next lease.

Uses a fake page factory (no playwright) so the fan-out can be driven fast.
"""

from __future__ import annotations

import asyncio

import pytest

from webclient import WebClient, wq
from webclient.clients import ClientPool, HTTPXFactory
from webclient.clients.base import ClientFactory
from webclient.clients.browser import BrowserClient, PageResult
from webclient.collection import Collection


class _FakePage:
    def __init__(self, url: str) -> None:
        self.url = url


class _FakeBrowserClient(BrowserClient):
    kind = "page"

    def __init__(self) -> None:
        self.page = _FakePage("about:blank")

    async def open(self, url, *, scripts=(), replay=[], wait=None):  # type: ignore[override]
        await asyncio.sleep(0.02)  # let siblings pile up so accumulation would bite
        return PageResult(url, b"<html><body>ok</body></html>", console=[], network=[])

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


@pytest.fixture
def fake_wc():
    wc = WebClient(timeout=5.0)
    wc._pool = ClientPool(
        {"http": HTTPXFactory(), "page": _FakeBrowserFactory()},
        limits={"http": 10, "page": 2},  # tiny cap so > cap is easy to hit
        acquire_timeout=8.0,
    )
    try:
        yield wc
    finally:
        wc.close()


def _urls(httpserver, prefix: str, n: int) -> list[str]:
    urls = []
    for i in range(n):
        httpserver.expect_request(f"/{prefix}{i}").respond_with_data(
            "<html><body>ok</body></html>", content_type="text/html"
        )
        urls.append(httpserver.url_for(f"/{prefix}{i}"))
    return urls


def test_plan_resolving_more_browser_pages_than_pool_completes(fake_wc, httpserver):
    n = 6  # 3x the page cap (2)
    col = Collection([fake_wc.ref(u) for u in _urls(httpserver, "p", n)], client=fake_wc)
    plan = wq.doc.resolve(browser=True).status_code
    out = fake_wc.execute(plan, col)  # would raise TimeoutError before the fix
    assert list(out) == [200] * n
    s = fake_wc.pool.stats()
    assert s.pages_free <= s.pages_total
    assert fake_wc.pool._held.get("page", 0) == 0  # every plan page released


def test_streamed_plan_over_many_browser_pages_completes(fake_wc, httpserver):
    n = 5  # > page cap (2)
    col = Collection([fake_wc.ref(u) for u in _urls(httpserver, "s", n)], client=fake_wc)
    plan = wq.doc.resolve(browser=True).status_code
    rows = list(fake_wc.execute(plan, col, stream=True))
    assert sorted(rows) == [200] * n
    assert fake_wc.pool._held.get("page", 0) == 0


def test_fanout_width_is_bounded_by_the_page_pool_for_browser_branches(fake_wc):
    # a fan-out whose branches each lease a browser page is capped at the PAGE pool
    # (2 here), not the http width (10) -- so it doesn't schedule 10 tasks that queue
    # behind the smaller page semaphore. A static branch keeps the http width.
    from webclient.query.executor import _fanout_limit

    browser_steps = wq.doc.resolve(browser=True).status_code._plan.steps
    static_steps = wq.doc.resolve().status_code._plan.steps
    auto_steps = wq.doc.resolve(browser="auto").status_code._plan.steps
    assert _fanout_limit(fake_wc, browser_steps) == 2  # bounded by the page pool
    assert _fanout_limit(fake_wc, static_steps) == 10  # http width
    assert _fanout_limit(fake_wc, auto_steps) == 10  # auto isn't a definite page lease
    assert _fanout_limit(fake_wc, None) == 10  # no steps -> http width
