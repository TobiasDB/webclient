"""web.dsl tests -- the four dispatch modes over one recorded plan."""

from __future__ import annotations

import asyncio

from pytest_httpserver import HTTPServer

from web.fetch import HttpFetcher
from web.resolve import Resolver
from web.dsl import DSL, Lazy, Plan, run_blob


def _run(coro):
    return asyncio.run(coro)


def _dsl() -> DSL:
    return DSL(Resolver(HttpFetcher()))


def test_lazy_records_a_plan_without_executing(httpserver: HTTPServer) -> None:
    lazy = _dsl().get(httpserver.url_for("/p")).select("h1")
    assert isinstance(lazy, Lazy)
    assert lazy.plan.steps[0].op == "select" and lazy.plan.steps[0].args == ["h1"]
    # nothing was requested -- purely lazy (no expectation registered, no call made)


def test_sync_and_async_dispatch_same_plan(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/p").respond_with_data(b"<h1>Hi</h1>", content_type="text/html")

    async def go_async() -> str:
        d = _dsl()
        try:
            els = await d.get(httpserver.url_for("/p")).select("h1").acollect()
            return els[0].text
        finally:
            await d.aclose()

    # SYNC dispatch
    d = _dsl()
    try:
        els = d.get(httpserver.url_for("/p")).select("h1").collect()
        assert els[0].text == "Hi"
    finally:
        _run(d.aclose())
    # ASYNC dispatch, same recorded chain
    assert _run(go_async()) == "Hi"


def test_api_dispatch_roundtrips_a_blob(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/p").respond_with_data(b"<title>T</title>", content_type="text/html")

    # client side: build a plan and serialise it (no execution, no resolver needed to record)
    blob = _dsl().get(httpserver.url_for("/p")).select("title").to_blob()
    assert '"op":"select"' in blob and Plan.from_blob(blob).steps[0].op == "select"

    # server side: run the blob against a local resolver
    async def server() -> str:
        r = Resolver(HttpFetcher())
        try:
            els = await run_blob(blob, r)
            return els[0].text
        finally:
            await r.aclose()

    assert _run(server()) == "T"
