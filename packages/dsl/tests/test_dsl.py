"""web.dsl tests -- typed surfaces, clean joins, and the dispatch modes."""

from __future__ import annotations

import asyncio

import pytest
from pytest_httpserver import HTTPServer

from web.fetch import HttpFetcher
from web.kernel import WebException
from web.resolve import Resolver
from web.dsl import DSL, Document, Plan, Reference, run_blob


def _run(coro):
    return asyncio.run(coro)


def _dsl() -> DSL:
    return DSL(Resolver())


def test_surfaces_and_join_record_a_plan() -> None:
    lazy = _dsl().ref("https://x/").click("#more").type("#q", "hi").doc().select_all(".row")
    assert isinstance(lazy, Document)
    plan = lazy._full()
    assert [s.op for s in plan.actions] == ["click", "type"]  # Reference actions
    assert plan.actions[1].args == ["#q", "hi"]
    assert [s.op for s in plan.reads] == ["select_all"]  # Document reads after .doc()


def test_actions_return_self_surface() -> None:
    r = _dsl().ref("https://x/")
    assert isinstance(r.click("#a"), Reference)  # actions return the Reference surface (Self)
    assert isinstance(r.click("#a").type("#b", "c"), Reference)


def test_sync_and_async_dispatch_same_plan(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/p").respond_with_data(b"<h1>Hi</h1>", content_type="text/html")

    async def go_async() -> str:
        d = _dsl()
        try:
            els = await d.ref(httpserver.url_for("/p")).doc().select_all("h1").acollect()
            return els[0].text
        finally:
            await d.aclose()

    d = _dsl()
    try:
        els = d.ref(httpserver.url_for("/p")).doc().select_all("h1").collect()  # SYNC
        assert els[0].text == "Hi"
    finally:
        _run(d.aclose())
    assert _run(go_async()) == "Hi"  # ASYNC, same chain


def test_api_dispatch_roundtrips_a_blob(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/p").respond_with_data(b"<title>T</title>", content_type="text/html")
    blob = _dsl().ref(httpserver.url_for("/p")).doc().select_all("title").to_blob()
    assert Plan.from_blob(blob).reads[0].op == "select_all"

    async def server() -> str:
        r = Resolver()
        try:
            els = await run_blob(blob, r)
            return els[0].text
        finally:
            await r.aclose()

    assert _run(server()) == "T"


def test_reference_actions_need_a_browser_for_now() -> None:
    async def go() -> None:
        d = _dsl()
        try:
            await d.ref("https://x/").click("#more").doc().text().acollect()
        finally:
            await d.aclose()

    with pytest.raises(WebException) as ei:
        _run(go())
    assert ei.value.error.code == "dsl.needs_browser"


def test_crawl_surface(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/").respond_with_data(b"<a href='/a'>a</a>", content_type="text/html")
    httpserver.expect_request("/a").respond_with_data(b"<p>a</p>", content_type="text/html")

    async def go() -> int:
        d = _dsl()
        docs = await d.crawl([httpserver.url_for("/")], max_pages=5).acollect()
        await d.aclose()
        return len(docs)

    assert _run(go()) >= 2


def test_reference_actions_drive_a_browser(httpserver: HTTPServer) -> None:
    from web.fetch import BrowserFetcher
    httpserver.expect_request("/f").respond_with_data(
        b"<html><body><input id='q'>"
        b"<button id='go' onclick=\"document.body.setAttribute('data-done', document.getElementById('q').value)\">go</button>"
        b"</body></html>", content_type="text/html")

    async def go() -> "str | None":
        d = DSL(Resolver(), browser=BrowserFetcher())
        try:  # actions drive one live page, .doc() snapshots+parses once, then read
            els = await d.ref(httpserver.url_for("/f")).type("#q", "hi").click("#go").doc().select_all("body").acollect()
            return els[0].attr("data-done")
        finally:
            await d.aclose()

    assert _run(go()) == "hi"


def test_fanout_maps_reads_over_a_collection(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/list").respond_with_data(
        b"<ul><li class='row'><a class='t' href='/a'>A</a></li>"
        b"<li class='row'><a class='t' href='/b'>B</a></li></ul>", content_type="text/html")

    async def go() -> tuple[list[str], list[dict]]:
        d = _dsl()
        try:
            base = d.ref(httpserver.url_for("/list")).doc()
            texts = await base.select_all(".row .t").text().acollect()      # map text over the collection
            rows = await base.select_all(".row").project(title=".t").acollect()  # row dicts
            return texts, rows
        finally:
            await d.aclose()

    texts, rows = _run(go())
    assert texts == ["A", "B"]
    assert rows == [{"title": "A"}, {"title": "B"}]
