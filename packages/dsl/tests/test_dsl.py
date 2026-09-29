"""web.dsl tests -- the ``wq`` surface, typed Collection/Field, and the four dispatch modes."""

from __future__ import annotations

import asyncio
from typing import Any

from pytest_httpserver import HTTPServer
from web.dsl import Collection, Field, Plan, WebClient, from_blob, run_blob, wq
from web.resolve import Resolver

_SHOP = (
    b"<html><body><main>"
    b'<div class="card"><span class="title">Aeropress</span><a class="link" href="/i/1">v</a>'
    b'<span class="price">$39</span></div>'
    b'<div class="card"><span class="title">Grinder</span><a class="link" href="/i/2">v</a>'
    b'<span class="price">$129</span></div>'
    b'<div class="card"><span class="title">Free Sample</span><a class="link" href="/i/3">v</a>'
    b'<span class="price"></span></div>'
    b"</main></body></html>"
)
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
    q = (
        wq.reference("https://x/")
        .resolve()
        .select_all(".card")
        .extract(title=wq.doc.select(".title").attr("text"))
        .project()
    )
    blob = q.to_blob()
    plan = Plan.from_blob(blob).validate_names()
    assert plan.source == "https://x/"
    assert [s.name for s in plan.steps if s.kind == "get"] == [
        "resolve",
        "select_all",
        "extract",
        "project",
    ]
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
    chain = (
        wq.reference(url)
        .resolve()
        .select_all(".card")
        .extract(title=wq.doc.select(".title").attr("text"))
    )

    rows_async = _run(chain.acollect())
    rows_sync = chain.collect()
    assert rows_async == rows_sync
    assert [r["title"] for r in rows_async] == ["Aeropress", "Grinder", "Free Sample"]


def test_blob_dispatch_runs_server_side(httpserver: HTTPServer) -> None:
    url = _shop(httpserver)
    blob = (
        wq.reference(url)
        .resolve()
        .select_all(".card")
        .extract(title=wq.doc.select(".title").attr("text"))
        .to_blob()
    )
    rows = _run(run_blob(blob))
    assert [r["title"] for r in rows] == ["Aeropress", "Grinder", "Free Sample"]


# -- extract / filter / when / field ----------------------------------------


def test_extract_filter_number_and_smart_collect(httpserver: HTTPServer) -> None:
    url = _shop(httpserver)
    # no explicit .project(): collect is smart and returns the rows
    rows = _run(
        wq.reference(url)
        .resolve()
        .select_all(".card")
        .extract(
            title=wq.doc.select(".title").attr("text"),
            price=wq.doc.select(".price").attr("text").number(default=0),
        )
        .filter(wq.doc.select(".price").attr("text") != "")
        .acollect()
    )
    assert rows == [
        {"title": "Aeropress", "price": 39},
        {"title": "Grinder", "price": 129},
    ]


def test_when_then_without_otherwise_defaults_to_none(httpserver: HTTPServer) -> None:
    url = _shop(httpserver)
    rows = _run(
        wq.reference(url)
        .resolve()
        .select_all(".card")
        .extract(
            name=wq.doc.select(".title").attr("text"),
            tier=wq.when(wq.doc.select(".price").attr("text") != "").then("priced"),
        )  # no .otherwise()
        .acollect()
    )
    assert [(r["name"], r["tier"]) for r in rows] == [
        ("Aeropress", "priced"),
        ("Grinder", "priced"),
        ("Free Sample", None),
    ]  # falsy -> None


def test_wc_when_and_field(httpserver: HTTPServer) -> None:
    url = _shop(httpserver)

    async def go() -> list[Any]:
        async with WebClient() as wc:
            return await (
                wc.resolve(url)
                .select_all(".card")
                .extract(
                    name=wq.doc.select(".title").attr("text"),
                    tier=wc.when(wq.doc.select(".price").attr("text") != "")
                    .then("y")
                    .otherwise("n"),
                )
                .filter(wc.field("name") != "Grinder")
                .acollect()
            )

    assert [(r["name"], r["tier"]) for r in _run(go())] == [
        ("Aeropress", "y"),
        ("Free Sample", "n"),
    ]


def test_select_miss_is_loud_by_default(httpserver: HTTPServer) -> None:
    import pytest
    from web.fetch import WebException

    url = _shop(httpserver)
    with pytest.raises(WebException):  # a selector matching nothing raises, naming it
        _run(wq.reference(url).resolve().select(".nope").attr("text").acollect())
    # ...unless marked optional -> the miss is None and the chain short-circuits
    got = _run(wq.reference(url).resolve().select(".nope", optional=True).attr("text").acollect())
    assert got is None


def test_when_then_otherwise_labels_rows(httpserver: HTTPServer) -> None:
    url = _shop(httpserver)
    rows = _run(
        wq.reference(url)
        .resolve()
        .select_all(".card")
        .extract(
            title=wq.doc.select(".title").attr("text"),
            tier=wq.when(wq.doc.select(".price").attr("text") != "")
            .then("priced")
            .otherwise("free"),
        )
        .acollect()
    )
    assert [(r["title"], r["tier"]) for r in rows] == [
        ("Aeropress", "priced"),
        ("Grinder", "priced"),
        ("Free Sample", "free"),
    ]


def test_field_references_an_earlier_column(httpserver: HTTPServer) -> None:
    url = _shop(httpserver)
    rows = _run(
        wq.reference(url)
        .resolve()
        .select_all(".card")
        .extract(name=wq.doc.select(".title").attr("text"))
        .filter(wq.field("name") != "Grinder")
        .acollect()
    )
    assert [r["name"] for r in rows] == ["Aeropress", "Free Sample"]


# -- attr fan-out + documents follow -----------------------------------------


def test_attr_fanout_returns_references(httpserver: HTTPServer) -> None:
    url = _shop(httpserver)
    refs = _run(wq.reference(url).resolve().select_all(".card a.link").attr("href").acollect())
    assert [r.url.rsplit("/", 1)[-1] for r in refs] == ["1", "2", "3"]  # a list of Ref
    # a fanned text read stays a chainable collection: .text().number() works
    prices = _run(wq.reference(url).resolve().select_all(".card .price").text().acollect())
    assert prices == ["$39", "$129", ""]


def test_attr_href_is_a_resolvable_reference(httpserver: HTTPServer) -> None:
    url = _shop(httpserver)
    # single: select a link, attr('href') -> a Ref, .resolve() follows it into the detail page
    sku = _run(
        wq.reference(url)
        .resolve()
        .select(".card a.link")
        .attr("href")
        .resolve()
        .select("h1.sku")
        .attr("text")
        .acollect()
    )
    assert sku == "SKU-1"
    # fanned: attr('href') over a collection -> resolve() ALL -> a collection of detail docs
    skus = _run(
        wq.reference(url)
        .resolve()
        .select_all(".card a.link")
        .attr("href")
        .resolve()
        .select("h1.sku")
        .attr("text")
        .acollect()
    )
    assert skus == ["SKU-1", "SKU-2", "SKU-3"]


def test_lazy_resolve_takes_a_named_profile_and_policy(httpserver: HTTPServer) -> None:
    url = _shop(httpserver)
    # resolve mirrors the resolve() signature: a named profile + per-step policy (JSON-safe, so the
    # plan still serialises)
    chain = (
        wq.reference(url)
        .resolve(profile="basic", retry=1, raise_on_error=True)
        .select(".card .title")
        .attr("text")
    )
    assert chain.collect() == "Aeropress"
    assert '"resolve"' in chain.to_blob()  # policy args recorded, plan stays serialisable


def test_extract_follows_a_reference_into_detail_pages(httpserver: HTTPServer) -> None:
    url = _shop(httpserver)
    # per-element enrichment: extract a link, then resolve it and read the detail page's SKU
    rows = _run(
        wq.reference(url)
        .resolve()
        .select_all(".card")
        .extract(link=wq.doc.select("a.link").attr("href"))
        .extract(sku=wq.doc.reference("link").resolve().select("h1.sku").attr("text"))
        .acollect()
    )
    assert [r["sku"] for r in rows] == ["SKU-1", "SKU-2", "SKU-3"]


# -- WebClient (context-managed DSL entry) -----------------------------------


def test_same_verbs_query_a_json_data_api(httpserver: HTTPServer) -> None:
    body = b'{"data": {"items": [{"sku": "W1", "p": {"n": "Widget"}}, {"sku": "G2", "p": {"n": "Gadget"}}]}}'
    httpserver.expect_request("/api").respond_with_data(body, content_type="application/json")
    # IDENTICAL to the HTML form -- select_all navigates the array, extract reads each item's leaves;
    # only the selector dialect differs (a JSON path instead of CSS)
    rows = _run(
        wq.reference(httpserver.url_for("/api"))
        .resolve()
        .select_all("data.items")
        .extract(
            sku=wq.doc.select("sku").attr("text"),
            name=wq.doc.select("p.n").attr("text"),
        )
        .acollect()
    )
    assert rows == [{"sku": "W1", "name": "Widget"}, {"sku": "G2", "name": "Gadget"}]


def test_webclient_is_context_managed(httpserver: HTTPServer) -> None:
    url = _shop(httpserver)

    async def go() -> Any:
        async with WebClient() as wc:
            doc = await wc.resolve(url).doc().acollect()  # a resolved Document
            rows = await (
                wc.resolve(url)
                .select_all(".card")
                .extract(title=wq.doc.select(".title").attr("text"))
                .acollect()
            )
            return doc.metadata().title, [r["title"] for r in rows]

    title, titles = _run(go())
    assert titles == ["Aeropress", "Grinder", "Free Sample"]


def test_resolver_is_an_async_context_manager(httpserver: HTTPServer) -> None:
    url = _shop(httpserver)

    async def go() -> list[Any]:
        async with Resolver() as rs:
            return await (
                wq.reference(url)
                .resolve()
                .select_all(".card")
                .extract(t=wq.doc.select(".title").attr("text"))
                .acollect(resolver=rs)
            )

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
