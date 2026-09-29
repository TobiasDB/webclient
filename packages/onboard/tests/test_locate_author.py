"""Locate + Author -- the reusable, registry-driven phases (no LLM), on the wq DSL.

Covers: Locate selecting a dataset candidate; Locate PREFERRING a consistent XHR/data-API JSON
endpoint over the page; Author emitting a WORKING wq query for an HTML repeating-record list, a
JSON data-API, and an HTML header table; and the composition end to end.
"""

from __future__ import annotations

import asyncio
from typing import cast

from pytest_httpserver import HTTPServer

from web.resolve import Resolver
from web.onboard import (DatasetBrief, LocateBrief, Reference, author, build_query, locate,
                         locate_and_author)
from web.onboard.patterns import best_pattern


def _run(coro: object) -> object:
    return asyncio.run(cast("asyncio.Future[object]", coro))


_PEOPLE = (b"<html><body><ul class='team'>"
           b"<li class='row'><span class='name'>Alice</span><span class='role'>CEO</span></li>"
           b"<li class='row'><span class='name'>Bob</span><span class='role'>CTO</span></li>"
           b"<li class='row'><span class='name'>Cara</span><span class='role'>COO</span></li>"
           b"</ul></body></html>")


def test_locate_selects_the_record_list_candidate(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/people").respond_with_data(_PEOPLE, content_type="text/html")

    async def go() -> "Reference | None":
        async with Resolver() as r:
            return await locate(LocateBrief(goal="team members",
                                            candidates=[httpserver.url_for("/people")]), resolver=r)

    ref = cast("Reference | None", _run(go()))
    assert ref is not None
    assert ref.kind == "html" and ref.record_selector == "li.row"
    assert "record_list" in ref.flags       # the whole flag surface fired on the page


def test_locate_prefers_a_consistent_xhr_data_api(httpserver: HTTPServer) -> None:
    # the page renders the products AND declares the JSON data-API that backs it
    page = (b"<html><head><link rel='alternate' type='application/json' href='/api/products.json'>"
            b"</head><body><ul><li class='row'><span class='name'>Widget</span></li>"
            b"<li class='row'><span class='name'>Gadget</span></li>"
            b"<li class='row'><span class='name'>Sprocket</span></li></ul></body></html>")
    httpserver.expect_request("/shop").respond_with_data(page, content_type="text/html")
    httpserver.expect_request("/api/products.json").respond_with_data(
        b'[{"name":"Widget","price":9},{"name":"Gadget","price":12},{"name":"Sprocket","price":7}]',
        content_type="application/json")

    async def go() -> "Reference | None":
        async with Resolver() as r:
            return await locate(LocateBrief(goal="products", candidates=[httpserver.url_for("/shop")]),
                                resolver=r)

    ref = cast("Reference | None", _run(go()))
    assert ref is not None
    # the XHR rule fired: the located source is the JSON endpoint, not the HTML page
    assert ref.url == httpserver.url_for("/api/products.json")
    assert ref.kind == "json" and ref.api_endpoint == httpserver.url_for("/api/products.json")
    assert ref.page_url == httpserver.url_for("/shop")  # provenance kept


def test_author_writes_a_working_query_for_a_record_list(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/people").respond_with_data(_PEOPLE, content_type="text/html")
    ref = Reference(url=httpserver.url_for("/people"), kind="html", record_selector="li.row",
                    flags=["record_list"])

    async def go() -> object:
        async with Resolver() as r:
            q, pattern, _notes = await build_query(ref, DatasetBrief(fields=["name", "role"]), resolver=r)
            assert pattern == "repeating_records"
            return await q.acollect(resolver=r)

    rows = cast("list[dict[str, object]]", _run(go()))
    assert rows == [{"name": "Alice", "role": "CEO"},
                    {"name": "Bob", "role": "CTO"},
                    {"name": "Cara", "role": "COO"}]


def test_author_writes_a_working_query_for_a_json_data_api(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/api/items").respond_with_data(
        b'{"results":[{"name":"Widget","price":{"amount":9}},{"name":"Gadget","price":{"amount":12}}]}',
        content_type="application/json")
    ref = Reference(url=httpserver.url_for("/api/items"), kind="json",
                    api_endpoint=httpserver.url_for("/api/items"))

    async def go() -> object:
        async with Resolver() as r:
            q, pattern, _notes = await build_query(
                ref, DatasetBrief(fields=["name", "amount"], selectors={"amount": "price.amount"}), resolver=r)
            assert pattern == "json_array"
            return await q.acollect(resolver=r)

    rows = cast("list[dict[str, object]]", _run(go()))
    assert rows == [{"name": "Widget", "amount": 9}, {"name": "Gadget", "amount": 12}]


def test_author_writes_a_working_query_for_an_html_table(httpserver: HTTPServer) -> None:
    table = (b"<html><body><table><tr><th>name</th><th>city</th></tr>"
             b"<tr><td>Ada</td><td>London</td></tr>"
             b"<tr><td>Linus</td><td>Helsinki</td></tr>"
             b"<tr><td>Grace</td><td>New York</td></tr></table></body></html>")
    httpserver.expect_request("/tbl").respond_with_data(table, content_type="text/html")
    ref = Reference(url=httpserver.url_for("/tbl"), kind="html", record_selector="tr")
    picked = best_pattern(ref)
    assert picked is not None and picked.name == "html_table"  # routed to the table pattern

    async def go() -> object:
        async with Resolver() as r:
            q = await author(ref, DatasetBrief(fields=["name", "city"]), resolver=r)
            return await q.acollect(resolver=r)

    rows = cast("list[dict[str, object]]", _run(go()))
    # the header row has no <td>, so it projects empty; the data rows carry the record
    assert {"name": "Ada", "city": "London"} in rows
    assert {"name": "Linus", "city": "Helsinki"} in rows


def test_locate_and_author_compose_end_to_end(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/people").respond_with_data(_PEOPLE, content_type="text/html")

    async def go() -> object:
        async with Resolver() as r:
            q = await locate_and_author(
                LocateBrief(goal="team", candidates=[httpserver.url_for("/people")]),
                DatasetBrief(fields=["name", "role"]), resolver=r)
            assert q is not None
            return await q.acollect(resolver=r)

    rows = cast("list[dict[str, object]]", _run(go()))
    assert {"name": "Alice", "role": "CEO"} in rows
