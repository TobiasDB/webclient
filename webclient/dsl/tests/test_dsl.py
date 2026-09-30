"""web.dsl tests -- the ``wq`` surface, typed Collection/Field, and the four dispatch modes."""

from __future__ import annotations

import asyncio
from typing import cast
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


def plain(value: object) -> object:
    """Rows without the implicit identity columns (`_identity` / `_url`) -- exact comparisons."""
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


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


def test_to_source_round_trips_through_from_source() -> None:
    # the FUNCTIONAL representation: a query serialises to a runnable wq source string and back.
    from web.dsl import SourceError, from_source

    q = (
        wq.reference("https://x/")
        .resolve(profile="basic")
        .select_all(".card")
        .extract(
            title=wq.doc.select(".title").attr("text"),
            price=wq.doc.select(".price").attr("text").number(default=0),
        )
        .filter(wq.doc.field("price") != "")
    )
    src = q.to_source()
    assert src.startswith("wq.reference('https://x/').resolve(profile='basic').select_all(")
    assert from_source(src).to_blob() == q.to_blob()  # rebuilds the identical plan
    # the safety boundary is preserved: no arbitrary code, no private attributes
    import pytest

    with pytest.raises(SourceError):
        from_source("wq.reference.__globals__['os']")
    with pytest.raises(SourceError):
        from_source("wq.doc.extract(x={'a': 1})")  # a dict literal is disallowed


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
    assert plain(rows) == [
        {"title": "Aeropress", "price": 39},
        {"title": "Grinder", "price": 129},
    ]


def test_extract_constant_column_yields_the_literal(httpserver: HTTPServer) -> None:
    # a bare-literal column (e.g. status="LISTED") must yield the CONSTANT for every row -- it used
    # to route through a plan wrapper that compared the element TO the literal, so it came back False.
    url = _shop(httpserver)
    rows = _run(
        wq.reference(url)
        .resolve()
        .select_all(".card")
        .extract(name=wq.doc.select(".title").attr("text"), status="LISTED")
        .acollect()
    )
    assert [r["status"] for r in rows] == ["LISTED", "LISTED", "LISTED"]
    assert rows[0]["name"] == "Aeropress"  # the sibling expression column still works


def test_resolve_is_memoised_per_run_so_a_detail_page_is_fetched_once(
    httpserver: HTTPServer,
) -> None:
    # Two detail-page columns follow the SAME record link. The DSL can't bind one resolved document
    # to many columns, so a naive query resolves the link once PER field; the executor memoises
    # policy-free resolves by URL within a run, so each item page is fetched ONCE, not per field.
    url = _shop(httpserver)
    rows = _run(
        wq.reference(url)
        .resolve()
        .select_all(".card")
        .extract(
            title=wq.doc.select(".title").attr("text"),
            sku=wq.doc.select("a.link").attr("href").resolve().select(".sku").attr("text"),
            sku_again=wq.doc.select("a.link").attr("href").resolve().select(".sku").attr("text"),
        )
        .acollect()
    )
    assert rows[0]["sku"] == rows[0]["sku_again"] == "SKU-1"
    for n in (1, 2, 3):  # each item page hit exactly once despite two resolves of it
        got = sum(1 for req, _ in httpserver.log if req.path == f"/i/{n}")
        assert got == 1, f"/i/{n} was fetched {got}x (resolve memo not applied)"


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
    assert plain(rows) == [{"sku": "W1", "name": "Widget"}, {"sku": "G2", "name": "Gadget"}]


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


def test_extract_after_a_per_record_resolve_fans_out_on_the_detail_page(
    httpserver: HTTPServer,
) -> None:
    # THE nested pattern: follow a record's link ONCE (`.attr('href').resolve()`) and fan out with
    # `.extract(...)` on the resolved page -- inside it wq.doc is the DETAIL page. It yields one
    # nested row per record, and each detail page is fetched exactly once.
    url = _shop(httpserver)
    rows = _run(
        wq.reference(url)
        .resolve()
        .select_all(".card")
        .extract(
            title=wq.doc.select(".title").attr("text"),
            detail=wq.doc.select("a.link")
            .attr("href")
            .resolve()
            .extract(
                sku=wq.doc.select(".sku").attr("text"),
                again=wq.doc.select(".sku").attr("text"),
            ),
        )
        .acollect()
    )
    assert rows[0]["title"] == "Aeropress"
    assert plain(rows[0]["detail"]) == {
        "sku": "SKU-1",
        "again": "SKU-1",
    }  # selected INSIDE the detail page
    assert [r["detail"]["sku"] for r in rows] == ["SKU-1", "SKU-2", "SKU-3"]
    for n in (1, 2, 3):
        assert sum(1 for req, _ in httpserver.log if req.path == f"/i/{n}") == 1  # one fetch each


def test_unknown_verbs_fail_loudly_at_parse_and_run_time() -> None:
    # A model-written `.join(...)` used to yield nulls silently. Now from_source names the gap (and
    # still reports every verb the chain used, for the tally), and a recorded chain with a verb the
    # value does not have raises `dsl.unknown_verb` at run time instead of returning None.
    from web.dsl import KNOWN_VERBS, UnknownVerb, from_source, verbs_of
    from web.fetch import WebException
    from web.parse import Document

    try:
        from_source('wq.doc.select_all("p").attr("text").join("\\n")')
    except UnknownVerb as exc:
        assert exc.verbs == ["join"] and exc.used == ["select_all", "attr", "join"]
    else:
        raise AssertionError("an unknown verb must be refused at parse time")
    assert {"select", "select_all", "attr", "extract", "filter", "limit", "resolve"} <= KNOWN_VERBS
    assert verbs_of(from_source('wq.doc.select_all("li").extract(a=wq.doc.attr("x"))')) == [
        "select_all",
        "extract",
        "attr",
    ]
    doc = Document(content=b"<p>hi</p>", kind="html")
    try:
        wq.doc.select("p").frobnicate().collect(doc)
    except WebException as exc:
        assert exc.error.code == "dsl.unknown_verb" and "frobnicate" in exc.error.message
    else:
        raise AssertionError("a runtime unknown verb must raise")


def test_resolve_memo_shares_fetches_across_runs(httpserver: HTTPServer) -> None:
    # Inside `resolve_memo()` several runs share one URL -> document memo (an author probing a
    # chain step by step fetches each detail page once, not once per probe).
    from web.dsl import resolve_memo
    from web.parse import Document
    from werkzeug.wrappers import Response

    calls = {"n": 0}

    def handler(_req: object) -> "Response":
        calls["n"] += 1
        return Response("<b class='x'>one</b>", content_type="text/html")

    httpserver.expect_request("/d").respond_with_handler(handler)
    listing = Document(content=f"<a href='{httpserver.url_for('/d')}'>go</a>".encode(), kind="html")
    chain = wq.doc.select("a").attr("href").resolve().select("b.x").attr("text")

    async def go() -> None:
        async with Resolver() as r:
            with resolve_memo():
                assert await chain.acollect(listing, resolver=r) == "one"
                assert await chain.acollect(listing, resolver=r) == "one"
            assert calls["n"] == 1  # the second run hit the memo
            await chain.acollect(listing, resolver=r)
            assert calls["n"] == 2  # outside the block each run fetches

    asyncio.run(go())


def test_field_regex_is_a_leaf_transform() -> None:
    # The query guide teaches `.regex(pattern, group=1)` on a leaf; only the document-level regex
    # existed, so a chain using it on a value raised (and, before the loud unknown-verb check,
    # silently read None). Found through the authoring verb record.
    from web.parse import Document

    doc = Document(
        content=b"<p class='h'>Storm hits coast, published at 17:09 BST</p>", kind="html"
    )
    read = wq.doc.select("p.h").attr("text")
    assert read.regex(r"at (\d\d:\d\d)", group=1).collect(doc) == "17:09"
    assert read.regex(r"never").collect(doc) is None
    assert read.regex(r"never", default="n/a").collect(doc) == "n/a"


def test_parse_when_reads_a_bare_time_as_today_and_fuzzy_prose() -> None:
    # USER: news timestamps come as "17:09 BST" -> TODAY at that time; a clock inside prose is
    # found (dateutil, fuzzy); a timezone abbreviation becomes an offset; prose with no digit
    # ("may lead to") is not a date. The exact forms (ISO / written / numeric / relative) still win.
    import datetime as dt

    from web.dsl.values import parse_when

    now = dt.datetime(2026, 9, 30, 12, 0, 0)
    got = parse_when("17:09 BST", now=now)
    assert got is not None and got.isoformat(timespec="seconds") == "2026-09-30T17:09:00+01:00"
    got = parse_when("Storm hits coast, published at 17:48 BST", now=now)
    assert got is not None and (got.hour, got.minute, got.date()) == (17, 48, now.date())
    got = parse_when("Published 30 September 2026, 10:12 BST", now=now)
    assert got is not None and got.isoformat(timespec="seconds").startswith("2026-09-30T10:12:00")
    assert parse_when("this may lead to more", now=now) is None
    assert parse_when("2 hours ago", now=now) == dt.datetime(2026, 9, 30, 10, 0, 0)
    assert parse_when("2026-09-30T16:26:51.555Z") is not None


def test_identity_verb_on_rows_and_documents(httpserver: HTTPServer) -> None:
    # USER: queries run daily; syncs are append-only -- so a record needs an IDENTITY, declared by
    # the author with ONE verb. `.identity()` after extract: every field; `.identity("title")`:
    # just those fields; a part that is not a field is a css selector resolved on the record and
    # hashed. A fanned-out DETAIL page gets its own `.identity("article")` -- stable across a clock
    # change -- and carries the url it came from. Nested as deep as the data goes.
    from web.dsl import IDENTITY_COLUMN, URL_COLUMN, identity_of
    from web.parse import Document
    from werkzeug.wrappers import Response

    clock = {"t": "10:00"}

    def article(_req: object) -> "Response":
        return Response(
            f"<html><body><nav>menu</nav><p class='clock'>{clock['t']}</p>"
            "<article><h1>T</h1><p>Body one.</p></article></body></html>",
            content_type="text/html",
        )

    httpserver.expect_request("/a/1").respond_with_handler(article)
    listing = Document(
        content=(
            f"<ul><li class='r'><span class='t'>T</span><span class='n'>3</span>"
            f"<a href='{httpserver.url_for('/a/1')}'>go</a></li></ul>"
        ).encode(),
        kind="html",
    )
    q = (
        wq.doc.select_all("li.r")
        .extract(
            title=wq.doc.select("span.t").attr("text"),
            views=wq.doc.select("span.n").attr("text").number(),
            detail=wq.doc.select("a")
            .attr("href")
            .resolve()
            .extract(body=wq.doc.select("article p").attr("text"))
            .identity("article"),
        )
        .identity("title")
    )
    assert ".identity('article')" in q.describe() and q.describe().endswith(".identity('title')")

    async def go() -> "tuple[dict[str, object], dict[str, object]]":
        async with Resolver() as r:
            first = await q.acollect(listing, resolver=r)
            clock["t"] = "11:00"  # the clock moved; the article did not
            second = await q.acollect(listing, resolver=r)
            return first[0], second[0]  # type: ignore[index]

    a, b = asyncio.run(go())
    assert a[IDENTITY_COLUMN] == identity_of({"title": "T"}, None, ["title"]) == b[IDENTITY_COLUMN]
    da = cast("dict[str, object]", a["detail"])
    db = cast("dict[str, object]", b["detail"])
    assert da[URL_COLUMN] == httpserver.url_for("/a/1")
    assert da[IDENTITY_COLUMN] == db[IDENTITY_COLUMN]
    assert da["body"] == "Body one." and "_doc" not in da
    # no parts: every extracted field counts (views included -> a changed count is a new record)
    rows = (
        wq.doc.select_all("li.r")
        .extract(
            title=wq.doc.select("span.t").attr("text"), views=wq.doc.select("span.n").attr("text")
        )
        .identity()
        .collect(listing)
    )
    assert rows[0][IDENTITY_COLUMN] == identity_of({"title": "T", "views": "3"})  # type: ignore[index]
    # a css part is resolved on the RECORD (not a field name)
    by_el = (
        wq.doc.select_all("li.r")
        .extract(title=wq.doc.select("span.t").attr("text"))
        .identity("span.n")
        .collect(listing)
    )
    expect = identity_of({"title": "T"}, listing.select("li.r"), ["span.n"])
    assert by_el[0][IDENTITY_COLUMN] == expect  # type: ignore[index]
    # the column form on a document
    page = Document(
        content=b"<article><p>Body one.</p></article><p class='clock'>10:00</p>", kind="html"
    )
    same = Document(
        content=b"<article><p>Body one.</p></article><p class='clock'>11:00</p>", kind="html"
    )
    assert wq.doc.identity("article").collect(page) == wq.doc.identity("article").collect(same)
    assert wq.doc.identity().collect(page) == wq.doc.identity().collect(same)  # main content


def test_from_source_accepts_a_chain_written_over_several_lines() -> None:
    # A chain laid out with leading-dot continuation lines (how a human -- or a model -- writes a
    # long query) is one expression; from_source joins the lines itself.
    from web.dsl import from_source

    src = """
wq.doc.select_all("li.row")
  .filter(wq.doc.select("a").attr("href").is_ok())
  .extract(
      name=wq.doc.select(".name").attr("text"),
  )
"""
    assert from_source(src).describe() == (
        "Document.select_all('li.row').filter(Document.select('a').attr('href').is_ok())"
        ".extract(name=Document.select('.name').attr('text'))"
    )


def test_a_reference_reads_as_a_field_for_the_leaf_verbs() -> None:
    # `attr("href")` yields a Ref (resolvable) -- it must still READ as a value: regex / link /
    # is_ok / split apply to its URL (a filter on the link's path, a link normalised), while
    # `.resolve()` keeps following it.
    from web.parse import Document

    doc = Document(
        content=b"<ul><li><a href='/articles/c1'>a</a></li><li><a href='/videos/v1'>v</a></li></ul>",
        kind="html",
        url="http://x/news",
    )
    rows = (
        wq.doc.select_all("li")
        .filter(wq.doc.select("a").attr("href").regex("/articles/").is_ok())
        .extract(url=wq.doc.select("a").attr("href"))
        .collect(doc)
    )
    assert plain(rows) == [{"url": "http://x/articles/c1"}]
    assert wq.doc.select("a").attr("href").link().collect(doc) == "http://x/articles/c1"
    assert (
        wq.doc.select("a").attr("href").regex(r"/(articles|videos)/", group=1).collect(doc)
        == "articles"
    )
