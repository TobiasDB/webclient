"""Scalability / resiliency (roadmap N15-N17): concurrent plans complete with a bounded pool
and no deadlock, resources are observable as ResourceEvents + a snapshot, one session cannot
starve the pool of pages (quota), and the remote transport retries per its RetryPolicy."""

import asyncio
import time

import pytest

from webclient import AsyncWebClient, BrowserConfig, ResourceEvent, WebClient, doc
from webclient.clients.pool import ClientPool
from webclient.lab import LabServer
from webclient.settings import LimitsSettings, Settings, use


@pytest.fixture(scope="module")
def lab():
    with LabServer() as srv:
        yield srv.base


def test_100_concurrent_plans_on_a_bounded_pool(lab):
    seen = []

    async def main():
        async with AsyncWebClient(browser_config=BrowserConfig(pool_http=4)) as ac:
            ac.bus.subscribe("resource", seen.append)
            plan = (ac.lazy.fetch(f"{lab}/lab/shop").select_all(".card")
                    .extract(title=doc.select(".title").attr("text")).project())
            started = time.perf_counter()
            results = await asyncio.gather(*(plan.acollect() for _ in range(100)))
            elapsed = time.perf_counter() - started
            stats = ac._the_engine().pool.stats()
            return results, elapsed, stats, ac.resources()

    results, elapsed, stats, res = asyncio.run(main())
    assert len(results) == 100 and all(len(r) == 3 for r in results)
    assert stats.http_free == stats.http_total and stats.waiting == 0 and stats.held.get("http", 0) == 0
    assert stats.http_total <= 4  # the pool never grew past its cap
    assert elapsed < 30
    assert any(isinstance(e, ResourceEvent) and e.detail.get("what") in ("wait", "created") for e in seen)
    assert res["pool"]["http_total"] <= 4 and res["rss_mb"] > 0 and res["mode"] == "async"


def test_health_reports_resources(lab):
    from fastapi.testclient import TestClient
    from webclient.service import create_app

    wc = WebClient()
    with TestClient(create_app(wc)) as api:
        wc.fetch(f"{lab}/lab/shop")
        h = api.get("/health").json()
        assert h["ok"] and "rss_mb" in h["resources"] and h["resources"]["events"] >= 1
    wc.close()


def test_per_owner_page_quota_is_fair():
    """A pool quota of 1 page per owner: owner A holding a page cannot take a second until it
    releases, while owner B still gets one -- and the quota event is published."""
    events = []

    class FakeClient:
        kind = "page"
        async def reset(self): pass
        async def aclose(self): pass

    class Factory:
        kind = "page"
        async def create(self): return FakeClient()
        async def aclose(self): pass

    async def main():
        pool = ClientPool({"page": Factory()}, limits={"page": 4}, per_owner={"page": 1},
                          acquire_timeout=2.0, on_event=lambda w, d: events.append((w, d)))
        a1 = await pool.lease("page", owner="A")
        b1 = await pool.lease("page", owner="B")  # another owner is unaffected
        second = asyncio.create_task(pool.lease("page", owner="A"))
        await asyncio.sleep(0.1)
        assert not second.done()  # A is at its quota
        assert pool.stats().owners == {"A": 1, "B": 1}
        await pool.release(a1)
        a2 = await asyncio.wait_for(second, 2.0)  # released -> A's second lease proceeds
        assert pool.stats().owners == {"A": 1, "B": 1}
        await pool.release(a2)
        await pool.release(b1)
        assert pool.stats().owners == {}
        # no quota when the owner is unknown / the cap is 0
        free = ClientPool({"page": Factory()}, limits={"page": 4})
        x = await free.lease("page")
        await free.release(x)

    asyncio.run(main())
    assert ("quota", {"kind": "page", "owner": "A", "cap": 1}) in events


def test_session_pages_setting_charges_the_session(httpserver):
    previous = use(Settings(limits=LimitsSettings(session_pages=1)))
    try:
        httpserver.expect_request("/a").respond_with_data("<html><title>a</title></html>", content_type="text/html")
        with WebClient(timeout=10.0) as wc:
            live = wc.ref(httpserver.url_for("/a")).resolve(browser=True).collect()
            assert wc.resources()["pool"]["owners"] == {"root": 1}
            wc.release(live)
            assert wc.resources()["pool"]["owners"] == {}
    finally:
        use(previous)


def test_keep_alive_page_has_an_idle_safety_net(httpserver):
    previous = use(Settings(limits=LimitsSettings(page_idle_ttl=0.3)))
    try:
        httpserver.expect_request("/k").respond_with_data("<html><title>k</title></html>", content_type="text/html")
        with WebClient(timeout=10.0) as wc:
            live = wc.fetch(httpserver.url_for("/k"), browser="always", keep_alive=True)
            assert live._page is not None
            deadline = time.time() + 5
            while live._page is not None and time.time() < deadline:
                time.sleep(0.1)
            assert live._page is None  # released by the safety net, not by the caller
    finally:
        use(previous)


def test_remote_transport_retries_per_policy(httpserver):
    from webclient.core.service import ServiceTransport
    from webclient.policy import RetryPolicy
    from werkzeug.wrappers import Response

    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return Response("busy", status=503, headers={"Retry-After": "0"})
        return Response('{"id": "s1", "status": "running"}', content_type="application/json")

    httpserver.expect_request("/sessions", method="POST").respond_with_handler(flaky)
    t = ServiceTransport(httpserver.url_for(""), None, 5.0, retry=RetryPolicy(max=3, base=0.01))
    assert t.open_session(None) == "s1" and calls["n"] == 3
    calls["n"] = 0
    strict = ServiceTransport(httpserver.url_for(""), None, 5.0, retry=RetryPolicy(max=0))
    with pytest.raises(Exception):
        strict.open_session(None)
    assert calls["n"] == 1
    t.close()
    strict.close()
