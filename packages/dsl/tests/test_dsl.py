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


def test_dsl_missing_select_does_not_crash(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/p").respond_with_data(b"<h1>Hi</h1>", content_type="text/html")

    async def go() -> object:
        d = _dsl()
        try:  # .select a missing node then read it -> None, not AttributeError
            return await d.ref(httpserver.url_for("/p")).doc().select(".nope").text().acollect()
        finally:
            await d.aclose()

    assert _run(go()) is None


# -- post-extraction transforms: shape the extracted data --

_SHOP = (b"<ul>"
         b"<li class=item><span class=t>Widget</span><span class=p>$1,299.00</span></li>"
         b"<li class=item><span class=t>Gadget</span><span class=p>$49</span></li>"
         b"<li class=item><span class=t>Widget</span><span class=p>$1,299.00</span></li>"
         b"<li class=item><span class=t></span><span class=p></span></li>"
         b"</ul>")


def test_transforms_clean_and_reshape_rows(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/s").respond_with_data(_SHOP, content_type="text/html")

    async def go() -> list:
        d = _dsl()
        try:
            return await (d.ref(httpserver.url_for("/s")).doc()
                          .select_all("li.item").project(title=".t", price=".p")
                          .nonempty().distinct(key="title").number(field="price")
                          .acollect())
        finally:
            await d.aclose()

    rows = _run(go())
    assert rows == [{"title": "Widget", "price": 1299.0}, {"title": "Gadget", "price": 49}]


def test_filter_and_limit(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/s").respond_with_data(_SHOP, content_type="text/html")

    async def go() -> list:
        d = _dsl()
        try:
            return await (d.ref(httpserver.url_for("/s")).doc()
                          .select_all("li.item").project(title=".t", price=".p")
                          .filter(title="widget").limit(1).acollect())
        finally:
            await d.aclose()

    rows = _run(go())
    assert rows == [{"title": "Widget", "price": "$1,299.00"}]  # substring, case-insensitive


def test_transforms_survive_a_blob_roundtrip(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/s").respond_with_data(_SHOP, content_type="text/html")
    blob = (_dsl().ref(httpserver.url_for("/s")).doc()
            .select_all("li.item").project(title=".t", price=".p")
            .nonempty().number(field="price").to_blob())

    async def server() -> list:
        r = Resolver()
        try:
            return await run_blob(blob, r)
        finally:
            await r.aclose()

    rows = _run(server())
    assert {"title": "Gadget", "price": 49} in rows and len(rows) == 3


def test_date_transform_normalises_to_iso(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/e").respond_with_data(
        b"<ul><li class=e>Posted March 3, 2026</li></ul>", content_type="text/html")

    async def go() -> list:
        d = _dsl()
        try:
            return await (d.ref(httpserver.url_for("/e")).doc()
                          .select_all("li.e").text().date().acollect())
        finally:
            await d.aclose()

    out = _run(go())
    assert out[0].startswith("2026-03-03")


def test_distinct_key_tolerates_unhashable_values() -> None:
    # distinct(key=...) on rows whose key column holds a list must not crash (unhashable marker)
    from web.dsl.transforms import apply as _apply
    rows = [{"tags": ["a", "b"], "id": 1}, {"tags": ["a", "b"], "id": 2}, {"tags": ["c"], "id": 3}]
    out = _apply(rows, "distinct", [{"field": None}])  # distinct with a key set below
    assert isinstance(out, list)
    keyed = _apply(rows, "distinct", ["tags"])  # key = "tags" (a list-valued column)
    assert keyed == [{"tags": ["a", "b"], "id": 1}, {"tags": ["c"], "id": 3}]  # deduped by list value


# -- project @attr + documents() follow-links (flatMap) --

def test_project_at_attr_extracts_attribute(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/l").respond_with_data(
        b"<ul><li class=item><a class=k href='/d/1'>One</a></li>"
        b"<li class=item><a class=k href='/d/2'>Two</a></li></ul>", content_type="text/html")

    async def go() -> list:
        d = _dsl()
        try:
            return await (d.ref(httpserver.url_for("/l")).doc()
                          .select_all("li.item").project(name="a.k", url="a.k@href").acollect())
        finally:
            await d.aclose()

    rows = _run(go())
    assert rows[0]["name"] == "One"
    assert rows[0]["url"] == httpserver.url_for("/d/1")   # @href resolved absolute
    assert rows[1]["url"] == httpserver.url_for("/d/2")


def test_documents_follows_url_column_and_flatmaps(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/l").respond_with_data(
        b"<ul><li class=item><a href='/d/1'>a</a></li><li class=item><a href='/d/2'>b</a></li></ul>",
        content_type="text/html")
    httpserver.expect_request("/d/1").respond_with_data(
        b"<html><body><h1 class=t>Detail One</h1></body></html>", content_type="text/html")
    httpserver.expect_request("/d/2").respond_with_data(
        b"<html><body><h1 class=t>Detail Two</h1></body></html>", content_type="text/html")

    async def go() -> list:
        d = _dsl()
        try:
            return await (d.ref(httpserver.url_for("/l")).doc()
                          .select_all("li.item").project(url="a@href")
                          .documents("url")                 # follow each detail URL
                          .select_all("h1.t").text()        # applied per detail page
                          .acollect())
        finally:
            await d.aclose()

    assert _run(go()) == ["Detail One", "Detail Two"]      # concatenated across detail pages


def test_documents_plan_roundtrips_through_blob(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/l").respond_with_data(
        b"<ul><li class=item><a href='/d/1'>a</a></li></ul>", content_type="text/html")
    httpserver.expect_request("/d/1").respond_with_data(
        b"<html><body><h1 class=t>Only</h1></body></html>", content_type="text/html")
    blob = (_dsl().ref(httpserver.url_for("/l")).doc()
            .select_all("li.item").project(url="a@href").documents("url")
            .select_all("h1.t").text().to_blob())

    async def server() -> list:
        r = Resolver()
        try:
            return await run_blob(blob, r)
        finally:
            await r.aclose()

    assert _run(server()) == ["Only"]   # follow + doc_reads survived serialisation
