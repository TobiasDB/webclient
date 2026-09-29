"""Locate + Author -- the reusable phases on the wq DSL.

Covers: Locate selecting a dataset candidate; Locate PREFERRING a consistent XHR/data-API JSON
endpoint over the page; Author driving a (scripted) LLM over the patterns guide to write a WORKING
wq query for an HTML repeating-record list, a JSON data-API, and an HTML header table; and the
composition end to end. The LLM is a scripted stub -- so these test the pipeline MECHANICS (prompt
-> parse -> reroot -> run), not a model's selector quality.
"""

from __future__ import annotations

import asyncio
from typing import cast

from pytest_httpserver import HTTPServer

from web.resolve import Resolver
from web.onboard import (DatasetBrief, LocateBrief, Reference, author, build_query, locate,
                         locate_and_author)


class ScriptedLlm:
    """An :class:`~web.onboard.Llm` that returns one canned ``wq`` reply, and records the prompt it
    was given -- a deterministic stand-in for a real model so Author is testable offline."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompt = ""

    async def complete(self, prompt: str) -> str:
        self.prompt = prompt
        return self.reply


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
    llm = ScriptedLlm('wq.doc.select_all("li.row").extract('
                      'name=wq.doc.select(".name").attr("text"), '
                      'role=wq.doc.select(".role").attr("text"))')

    async def go() -> object:
        async with Resolver() as r:
            q, engine, _notes = await build_query(ref, DatasetBrief(fields=["name", "role"]),
                                                  resolver=r, llm=llm)
            assert engine == "llm"
            return await q.acollect(resolver=r)

    rows = cast("list[dict[str, object]]", _run(go()))
    assert rows == [{"name": "Alice", "role": "CEO"},
                    {"name": "Bob", "role": "CTO"},
                    {"name": "Cara", "role": "COO"}]
    # the prompt carried the hardcoded flags AND the patterns guide
    assert "PAGE SIGNALS" in llm.prompt and "Writing a `wq` extraction query" in llm.prompt


def test_author_writes_a_working_query_for_a_json_data_api(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/api/items").respond_with_data(
        b'{"results":[{"name":"Widget","price":{"amount":9}},{"name":"Gadget","price":{"amount":12}}]}',
        content_type="application/json")
    ref = Reference(url=httpserver.url_for("/api/items"), kind="json",
                    api_endpoint=httpserver.url_for("/api/items"))
    llm = ScriptedLlm('wq.doc.select_all("results").extract('
                      'name=wq.doc.attr("name"), '
                      'amount=wq.doc.select("price.amount").attr("text").number())')

    async def go() -> object:
        async with Resolver() as r:
            q, engine, _notes = await build_query(ref, DatasetBrief(fields=["name", "amount"]),
                                                  resolver=r, llm=llm)
            assert engine == "llm"
            return await q.acollect(resolver=r)

    rows = cast("list[dict[str, object]]", _run(go()))
    assert rows == [{"name": "Widget", "amount": 9}, {"name": "Gadget", "amount": 12}]
    assert "JSON document" in llm.prompt  # the kind steer reached the prompt


def test_author_writes_a_working_query_for_an_html_table(httpserver: HTTPServer) -> None:
    table = (b"<html><body><table><thead><tr><th>name</th><th>city</th></tr></thead><tbody>"
             b"<tr><td>Ada</td><td>London</td></tr>"
             b"<tr><td>Linus</td><td>Helsinki</td></tr>"
             b"<tr><td>Grace</td><td>New York</td></tr></tbody></table></body></html>")
    httpserver.expect_request("/tbl").respond_with_data(table, content_type="text/html")
    ref = Reference(url=httpserver.url_for("/tbl"), kind="html", record_selector="tbody tr")
    llm = ScriptedLlm('wq.doc.select_all("tbody tr").extract('
                      'name=wq.doc.select("td:nth-of-type(1)").attr("text"), '
                      'city=wq.doc.select("td:nth-of-type(2)").attr("text"))')

    async def go() -> object:
        async with Resolver() as r:
            q = await author(ref, DatasetBrief(fields=["name", "city"]), resolver=r, llm=llm)
            return await q.acollect(resolver=r)

    rows = cast("list[dict[str, object]]", _run(go()))
    assert {"name": "Ada", "city": "London"} in rows
    assert {"name": "Linus", "city": "Helsinki"} in rows


def test_locate_and_author_compose_end_to_end(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/people").respond_with_data(_PEOPLE, content_type="text/html")
    llm = ScriptedLlm('wq.doc.select_all("li.row").extract('
                      'name=wq.doc.select(".name").attr("text"), '
                      'role=wq.doc.select(".role").attr("text"))')

    async def go() -> object:
        async with Resolver() as r:
            q = await locate_and_author(
                LocateBrief(goal="team", candidates=[httpserver.url_for("/people")]),
                DatasetBrief(fields=["name", "role"]), resolver=r, llm=llm)
            assert q is not None
            return await q.acollect(resolver=r)

    rows = cast("list[dict[str, object]]", _run(go()))
    assert {"name": "Alice", "role": "CEO"} in rows
