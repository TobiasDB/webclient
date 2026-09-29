"""web.dsl tests -- the ``wq`` surface, typed Collection/Field, and the four dispatch modes."""

from __future__ import annotations

import asyncio
from typing import Any

from pytest_httpserver import HTTPServer

from web.dsl import Collection, Field, Plan, WebClient, from_blob, run_blob, wq
from web.resolve import Resolver

_SHOP = (b"<html><body><main>"
         b'<div class="card"><span class="title">Aeropress</span><a class="link" href="/i/1">v</a>'
         b'<span class="price">$39</span></div>'
         b'<div class="card"><span class="title">Grinder</span><a class="link" href="/i/2">v</a>'
         b'<span class="price">$129</span></div>'
         b'<div class="card"><span class="title">Free Sample</span><a class="link" href="/i/3">v</a>'
         b'<span class="price"></span></div>'
         b"</main></body></html>")
_ITEM = b'<html><body><h1 class="sku">SKU-%d</h1></body></html>'


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _shop(server: HTTPServer) -> str:
    server.expect_request("/").respond_with_data(_SHOP, content_type="text/html")
    for n in (1, 2, 3):
        server.expect_request(f"/i/{n}").respond_with_data(_ITEM % n, content_type="text/html")
    return server.url_for("/")


# -- recording ---------------------------------------------------------------

def test_wq_records_a_serialisable_plan() -> None:
    q = wq.reference("https://x/").resolve().select_all(".card").extract(
        title=wq.doc.select(".title").attr("text")).project()
    blob = q.to_blob()
    plan = Plan.from_blob(blob).validate_names()
    assert plan.source == "https://x/"
    assert [s.name for s in plan.steps if s.kind == "get"] == ["resolve", "select_all", "extract", "project"]
    # the extract column is a nested sub-plan (per-element), not a literal -- kwargs ride the CALL
    # step that follows the "extract" get
    i = next(n for n, s in enumerate(plan.steps) if s.name == "extract")
    assert plan.steps[i + 1].kwargs["title"].plan is not None


def test_describe_round_trips_through_blob() -> None:
    q = wq.reference("https://x/").resolve().select_all(".card")
    assert "reference('https://x/')" in q.describe()
    assert from_blob(q.to_blob()).describe() == q.describe()


def test_lazy_expr_refuses_python_coercion() -> None:
    import pytest

    with pytest.raises(TypeError):
        bool(wq.doc.select(".x").attr("text"))


# -- the four dispatch modes -------------------------------------------------

def test_async_and_sync_collect_agree(httpserver: HTTPServer) -> None:
    url = _shop(httpserver)
    chain = wq.reference(url).resolve().select_all(".card").extract(
        title=wq.doc.select(".title").attr("text"))

    rows_async = _run(chain.acollect())
    rows_sync = chain.collect()
    assert rows_async == rows_sync
    assert [r["title"] for r in rows_async] == ["Aeropress", "Grinder", "Free Sample"]


def test_blob_dispatch_runs_server_side(httpserver: HTTPServer) -> None:
    url = _shop(httpserver)
    blob = (wq.reference(url).resolve().select_all(".card")
            .extract(title=wq.doc.select(".title").attr("text")).to_blob())
    rows = _run(run_blob(blob))
    assert [r["title"] for r in rows] == ["Aeropress", "Grinder", "Free Sample"]


# -- extract / filter / when / field ----------------------------------------

def test_extract_filter_number_and_smart_collect(httpserver: HTTPServer) -> None:
    url = _shop(httpserver)
    # no explicit .project(): collect is smart and returns the rows
    rows = _run(
        wq.reference(url).resolve().select_all(".card")
        .extract(title=wq.doc.select(".title").attr("text"),
                 price=wq.doc.select(".price").attr("text").number(default=0))
        .filter(wq.doc.select(".price").attr("text") != "")
        .acollect())
    assert rows == [{"title": "Aeropress", "price": 39}, {"title": "Grinder", "price": 129}]


def test_when_then_otherwise_labels_rows(httpserver: HTTPServer) -> None:
    url = _shop(httpserver)
    rows = _run(
        wq.reference(url).resolve().select_all(".card")
        .extract(title=wq.doc.select(".title").attr("text"),
                 tier=wq.when(wq.doc.select(".price").attr("text") != "").then("priced").otherwise("free"))
        .acollect())
    assert [(r["title"], r["tier"]) for r in rows] == [
        ("Aeropress", "priced"), ("Grinder", "priced"), ("Free Sample", "free")]


def test_field_references_an_earlier_column(httpserver: HTTPServer) -> None:
    url = _shop(httpserver)
    rows = _run(
        wq.reference(url).resolve().select_all(".card")
        .extract(name=wq.doc.select(".title").attr("text"))
        .filter(wq.field("name") != "Grinder")
        .acollect())
    assert [r["name"] for r in rows] == ["Aeropress", "Free Sample"]


# -- attr fan-out + documents follow -----------------------------------------

def test_attr_fanout_returns_a_list_of_values(httpserver: HTTPServer) -> None:
    url = _shop(httpserver)
    hrefs = _run(wq.reference(url).resolve().select_all(".card a.link").attr("href").acollect())
    assert [str(h).rsplit("/", 1)[-1] for h in hrefs] == ["1", "2", "3"]


def test_extract_follows_a_reference_into_detail_pages(httpserver: HTTPServer) -> None:
    url = _shop(httpserver)
    # per-element enrichment: extract a link, then resolve it and read the detail page's SKU
    rows = _run(
        wq.reference(url).resolve().select_all(".card")
        .extract(link=wq.doc.select("a.link").attr("href"))
        .extract(sku=wq.doc.reference("link").resolve().select("h1.sku").attr("text"))
        .acollect())
    assert [r["sku"] for r in rows] == ["SKU-1", "SKU-2", "SKU-3"]


# -- WebClient (context-managed DSL entry) -----------------------------------

def test_webclient_is_context_managed(httpserver: HTTPServer) -> None:
    url = _shop(httpserver)

    async def go() -> Any:
        async with WebClient() as wc:
            doc = await wc.resolve(url).doc().acollect()      # a resolved Document
            rows = await (wc.resolve(url).select_all(".card")
                          .extract(title=wq.doc.select(".title").attr("text")).acollect())
            return doc.metadata().title, [r["title"] for r in rows]

    title, titles = _run(go())
    assert titles == ["Aeropress", "Grinder", "Free Sample"]


def test_resolver_is_an_async_context_manager(httpserver: HTTPServer) -> None:
    url = _shop(httpserver)

    async def go() -> list[Any]:
        async with Resolver() as rs:
            return await (wq.reference(url).resolve().select_all(".card")
                          .extract(t=wq.doc.select(".title").attr("text")).acollect(resolver=rs))

    assert [r["t"] for r in _run(go())] == ["Aeropress", "Grinder", "Free Sample"]


# -- typed value leaves ------------------------------------------------------

def test_field_and_collection_helpers() -> None:
    assert Field("£51.77").number().get() == 51.77
    assert Field("Three").number().get() == 3
    assert Field("18 Sep 2026").date().get() == "2026-09-18"
    assert not Field("", ok=True).is_empty().get() is False  # empty string -> is_empty true
    coll: Collection[Field[str]] = Field("a, b, c").split(",")
    assert [f.get() for f in coll] == ["a", "b", "c"]
    assert len(coll) == 3
