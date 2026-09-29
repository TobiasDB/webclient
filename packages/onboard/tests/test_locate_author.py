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

import pytest
from pytest_httpserver import HTTPServer
from web.dsl import Plan
from web.resolve import Resolver

from web.onboard import (
    Brief,
    DatasetBrief,
    LocateBrief,
    Pricing,
    Reference,
    Usage,
    author,
    build_query,
    locate,
    locate_and_author,
)
from web.onboard.__main__ import main


class ScriptedLlm:
    """An :class:`~web.onboard.Llm` that returns one canned ``wq`` reply, and records the prompt it
    was given -- a deterministic stand-in for a real model so Author is testable offline.
    """

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompt = ""

    async def complete(self, prompt: str) -> str:
        self.prompt = prompt
        return self.reply


def _run(coro: object) -> object:
    return asyncio.run(cast("asyncio.Future[object]", coro))


_PEOPLE = (
    b"<html><body><ul class='team'>"
    b"<li class='row'><span class='name'>Alice</span><span class='role'>CEO</span></li>"
    b"<li class='row'><span class='name'>Bob</span><span class='role'>CTO</span></li>"
    b"<li class='row'><span class='name'>Cara</span><span class='role'>COO</span></li>"
    b"</ul></body></html>"
)


def test_locate_selects_the_record_list_candidate(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/people").respond_with_data(_PEOPLE, content_type="text/html")

    async def go() -> "Reference | None":
        async with Resolver() as r:
            return await locate(
                LocateBrief(goal="team members", candidates=[httpserver.url_for("/people")]),
                resolver=r,
            )

    ref = cast("Reference | None", _run(go()))
    assert ref is not None
    assert ref.kind == "html" and ref.record_selector == "li.row"
    assert "record_list" in ref.flags  # the whole flag surface fired on the page


_BLOCKED = (
    b"<html><body><h1>Access denied</h1><p>Please verify you are human.</p>"
    b"<table><tr><td>1</td><td>x</td></tr><tr><td>2</td><td>y</td></tr>"
    b"<tr><td>3</td><td>z</td></tr><tr><td>4</td><td>w</td></tr></table></body></html>"
)


def test_locate_rejects_a_blocked_bot_wall(httpserver: HTTPServer) -> None:
    # a bot-wall with a table is NOT a source, however table-like -- it must not be picked.
    httpserver.expect_request("/wall").respond_with_data(_BLOCKED, content_type="text/html")

    async def go() -> "Reference | None":
        async with Resolver() as r:
            return await locate(
                LocateBrief(goal="data", candidates=[httpserver.url_for("/wall")]), resolver=r
            )

    assert cast("Reference | None", _run(go())) is None


def test_locate_rejects_a_403_candidate(httpserver: HTTPServer) -> None:
    # status-aware: a 403 (even with a table) is not ok -> not a source, so locate finds nothing.
    httpserver.expect_request("/denied").respond_with_data(
        _BLOCKED, status=403, content_type="text/html"
    )

    async def go() -> "Reference | None":
        async with Resolver() as r:
            return await locate(
                LocateBrief(goal="data", candidates=[httpserver.url_for("/denied")]), resolver=r
            )

    assert cast("Reference | None", _run(go())) is None


def test_locate_reference_carries_flag_descriptions_and_signals(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/people").respond_with_data(_PEOPLE, content_type="text/html")

    async def go() -> "Reference | None":
        async with Resolver() as r:
            return await locate(
                LocateBrief(goal="team", candidates=[httpserver.url_for("/people")]), resolver=r
            )

    ref = cast("Reference | None", _run(go()))
    assert ref is not None
    rl = next((f for f in ref.assessment if f.name == "record_list"), None)
    assert rl is not None and rl.description and rl.confidence > 0.0  # the flag carries its meaning
    assert rl.signals and all(
        s.confidence > 0.0 for s in rl.signals
    )  # + the evidence + confidences


def test_locate_prefers_a_clean_source_over_a_blocked_one(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/wall").respond_with_data(_BLOCKED, content_type="text/html")
    httpserver.expect_request("/people").respond_with_data(_PEOPLE, content_type="text/html")

    async def go() -> "Reference | None":
        async with Resolver() as r:
            return await locate(
                LocateBrief(
                    goal="people",
                    candidates=[httpserver.url_for("/wall"), httpserver.url_for("/people")],
                ),
                resolver=r,
            )

    ref = cast("Reference | None", _run(go()))
    assert ref is not None and ref.url == httpserver.url_for("/people")


def test_locate_prefers_a_consistent_xhr_data_api(httpserver: HTTPServer) -> None:
    # the page renders the products AND declares the JSON data-API that backs it
    page = (
        b"<html><head><link rel='alternate' type='application/json' href='/api/products.json'>"
        b"</head><body><ul><li class='row'><span class='name'>Widget</span></li>"
        b"<li class='row'><span class='name'>Gadget</span></li>"
        b"<li class='row'><span class='name'>Sprocket</span></li></ul></body></html>"
    )
    httpserver.expect_request("/shop").respond_with_data(page, content_type="text/html")
    httpserver.expect_request("/api/products.json").respond_with_data(
        b'[{"name":"Widget","price":9},{"name":"Gadget","price":12},{"name":"Sprocket","price":7}]',
        content_type="application/json",
    )

    async def go() -> "Reference | None":
        async with Resolver() as r:
            return await locate(
                LocateBrief(goal="products", candidates=[httpserver.url_for("/shop")]),
                resolver=r,
            )

    ref = cast("Reference | None", _run(go()))
    assert ref is not None
    # the XHR rule fired: the located source is the JSON endpoint, not the HTML page
    assert ref.url == httpserver.url_for("/api/products.json")
    assert ref.kind == "json" and ref.api_endpoint == httpserver.url_for("/api/products.json")
    assert ref.page_url == httpserver.url_for("/shop")  # provenance kept


def test_author_writes_a_working_query_for_a_record_list(
    httpserver: HTTPServer,
) -> None:
    httpserver.expect_request("/people").respond_with_data(_PEOPLE, content_type="text/html")
    ref = Reference(
        url=httpserver.url_for("/people"),
        kind="html",
        record_selector="li.row",
        flags=["record_list"],
    )
    llm = ScriptedLlm(
        'wq.doc.select_all("li.row").extract('
        'name=wq.doc.select(".name").attr("text"), '
        'role=wq.doc.select(".role").attr("text"))'
    )

    async def go() -> object:
        async with Resolver() as r:
            q, engine, _notes = await build_query(
                ref, DatasetBrief(fields=["name", "role"]), resolver=r, llm=llm
            )
            assert engine == "llm"
            return await q.acollect(resolver=r)

    rows = cast("list[dict[str, object]]", _run(go()))
    assert rows == [
        {"name": "Alice", "role": "CEO"},
        {"name": "Bob", "role": "CTO"},
        {"name": "Cara", "role": "COO"},
    ]
    # the prompt carried the hardcoded flags AND the patterns guide
    assert "PAGE SIGNALS" in llm.prompt and "Writing a `wq` extraction query" in llm.prompt


def test_author_writes_a_working_query_for_a_json_data_api(
    httpserver: HTTPServer,
) -> None:
    httpserver.expect_request("/api/items").respond_with_data(
        b'{"results":[{"name":"Widget","price":{"amount":9}},{"name":"Gadget","price":{"amount":12}}]}',
        content_type="application/json",
    )
    ref = Reference(
        url=httpserver.url_for("/api/items"),
        kind="json",
        api_endpoint=httpserver.url_for("/api/items"),
    )
    llm = ScriptedLlm(
        'wq.doc.select_all("results").extract('
        'name=wq.doc.attr("name"), '
        'amount=wq.doc.select("price.amount").attr("text").number())'
    )

    async def go() -> object:
        async with Resolver() as r:
            q, engine, _notes = await build_query(
                ref, DatasetBrief(fields=["name", "amount"]), resolver=r, llm=llm
            )
            assert engine == "llm"
            return await q.acollect(resolver=r)

    rows = cast("list[dict[str, object]]", _run(go()))
    assert rows == [{"name": "Widget", "amount": 9}, {"name": "Gadget", "amount": 12}]
    assert "JSON document" in llm.prompt  # the kind steer reached the prompt


def test_author_writes_a_working_query_for_an_html_table(
    httpserver: HTTPServer,
) -> None:
    table = (
        b"<html><body><table><thead><tr><th>name</th><th>city</th></tr></thead><tbody>"
        b"<tr><td>Ada</td><td>London</td></tr>"
        b"<tr><td>Linus</td><td>Helsinki</td></tr>"
        b"<tr><td>Grace</td><td>New York</td></tr></tbody></table></body></html>"
    )
    httpserver.expect_request("/tbl").respond_with_data(table, content_type="text/html")
    ref = Reference(url=httpserver.url_for("/tbl"), kind="html", record_selector="tbody tr")
    llm = ScriptedLlm(
        'wq.doc.select_all("tbody tr").extract('
        'name=wq.doc.select("td:nth-of-type(1)").attr("text"), '
        'city=wq.doc.select("td:nth-of-type(2)").attr("text"))'
    )

    async def go() -> object:
        async with Resolver() as r:
            q = await author(ref, DatasetBrief(fields=["name", "city"]), resolver=r, llm=llm)
            return await q.acollect(resolver=r)

    rows = cast("list[dict[str, object]]", _run(go()))
    assert {"name": "Ada", "city": "London"} in rows
    assert {"name": "Linus", "city": "Helsinki"} in rows


def test_locate_and_author_compose_end_to_end(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/people").respond_with_data(_PEOPLE, content_type="text/html")
    llm = ScriptedLlm(
        'wq.doc.select_all("li.row").extract('
        'name=wq.doc.select(".name").attr("text"), '
        'role=wq.doc.select(".role").attr("text"))'
    )

    async def go() -> object:
        async with Resolver() as r:
            q = await locate_and_author(
                LocateBrief(goal="team", candidates=[httpserver.url_for("/people")]),
                DatasetBrief(fields=["name", "role"]),
                resolver=r,
                llm=llm,
            )
            assert q is not None
            return await q.acollect(resolver=r)

    rows = cast("list[dict[str, object]]", _run(go()))
    assert {"name": "Alice", "role": "CEO"} in rows


# -- Brief loading + the CLI ----------------------------------------------------------------------


def test_brief_loads_from_markdown_frontmatter() -> None:
    text = (
        "---\n"
        "name: team\n"
        "seeds: [https://acme.com/team]\n"
        "max_pages: 12\n"
        "schema:\n"
        "  - name: the person's full name\n"
        "  - role\n"
        "optional: [role]\n"
        "---\n"
        "Board members and their roles.\n"
    )
    brief = Brief.from_markdown(text)
    assert (
        brief.name == "team" and brief.seeds == ["https://acme.com/team"] and brief.max_pages == 12
    )
    assert brief.goal == "Board members and their roles."  # the body is the goal
    assert brief.fields == ["name", "role"]  # schema: -> fields
    assert brief.descriptions == {"name": "the person's full name"}  # {path: desc} -> descriptions
    assert brief.optional == ["role"]


class _ClosableLlm(ScriptedLlm):
    """A ScriptedLlm the CLI can close (it calls ``llm.aclose()``)."""

    async def aclose(self) -> None:
        return None


_REPLY = (
    'wq.doc.select_all("li.row").extract('
    'name=wq.doc.select(".name").attr("text"), role=wq.doc.select(".role").attr("text"))'
)


def _brief_file(tmp_path: object, url: str) -> str:
    """A temp brief markdown that points Locate at ``url`` (candidates: no search/crawl) with a
    name/role schema -- so the CLI (now brief-only) can be exercised offline."""
    from pathlib import Path

    text = (
        "---\n"
        "name: team\n"
        f'candidates: ["{url}"]\n'
        "schema:\n  - name\n  - role\n"
        "---\n"
        "team members\n"
    )
    path = Path(str(tmp_path)) / "team.md"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_cli_locate_emits_serialised_reference(
    httpserver: HTTPServer, capsys: "pytest.CaptureFixture[str]", tmp_path: object
) -> None:
    httpserver.expect_request("/people").respond_with_data(_PEOPLE, content_type="text/html")
    brief = _brief_file(tmp_path, httpserver.url_for("/people"))
    rc = main(["locate", brief, "--no-cache"])
    out = capsys.readouterr()
    assert rc == 0
    ref = Reference.model_validate_json(out.out.strip())  # stdout is the serialised Reference
    assert ref.record_selector == "li.row" and "record_list" in ref.flags
    assert "flags:" in out.err and "records:" in out.err  # reasoning went to stderr


def test_cli_author_locates_from_the_brief_and_runs(
    httpserver: HTTPServer,
    capsys: "pytest.CaptureFixture[str]",
    monkeypatch: "pytest.MonkeyPatch",
    tmp_path: object,
) -> None:
    # brief-only: `web author <brief>` with no cached reference locates from the brief itself.
    httpserver.expect_request("/people").respond_with_data(_PEOPLE, content_type="text/html")
    brief = _brief_file(tmp_path, httpserver.url_for("/people"))
    monkeypatch.setattr("web.onboard.__main__.AnthropicLlm", lambda **_k: _ClosableLlm(_REPLY))
    rc = main(["author", brief, "--run", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 0
    Plan.from_blob(out.out.strip())  # stdout is a rebuildable wq blob
    assert "query:" in out.err and "rows:" in out.err  # reasoning + sample went to stderr


def test_anthropic_meter_emits_a_live_llm_event() -> None:
    from web.fetch import Trace

    from web.onboard import AnthropicLlm, LlmEvent, Pricing

    llm = AnthropicLlm(pricing=Pricing(input=3.0, output=15.0))
    with Trace() as t:
        llm._meter({"usage": {"input_tokens": 1000, "output_tokens": 500}})
    events = [e for e in t.events if isinstance(e, LlmEvent)]
    assert len(events) == 1
    assert events[0].calls == 1
    assert abs(events[0].cost_usd - (1000 * 3.0 + 500 * 15.0) / 1_000_000) < 1e-12


def test_progress_streams_llm_cost_as_it_goes(capsys: "pytest.CaptureFixture[str]") -> None:
    from web.fetch import emit

    from web.onboard import LlmEvent
    from web.onboard.__main__ import _Progress

    with _Progress(verbose=False) as prog:
        emit(LlmEvent(model="haiku", calls=1, cost_usd=0.004, spent_usd=0.004))
        emit(LlmEvent(model="haiku", calls=2, cost_usd=0.006, spent_usd=0.010))
    assert prog.llm_calls == 2 and abs(prog.llm_spent - 0.010) < 1e-9
    err = capsys.readouterr().err
    assert "llm [haiku] call 1" in err and "running $0.0100" in err


def test_llm_frontier_middleware_picks_edges_by_model() -> None:
    from web.crawl import FrontierItem

    from web.onboard import llm_frontier

    items = tuple(FrontierItem(url=f"http://x/{i}") for i in range(4))
    mw = llm_frontier(ScriptedLlm("the picks are [2, 0]"), "find the data")

    async def nxt(pending: "tuple[FrontierItem, ...]") -> "list[FrontierItem]":
        return [pending[0]]  # FIFO fallback (should NOT be used here)

    picked = cast("list[FrontierItem]", _run(mw(items, nxt)))
    assert [it.url for it in picked] == ["http://x/2", "http://x/0"]  # model's order, subset


def test_llm_frontier_prompt_carries_link_text_and_parent_assessment() -> None:
    from web.crawl import FrontierItem

    from web.onboard import llm_frontier

    items = (
        FrontierItem(
            url="http://x/board",
            text="Board of Directors",
            parent="http://x/",
            parent_status=200,
            parent_title="About",
            parent_flags=["record_list"],
        ),
        FrontierItem(url="http://x/careers", text="Careers", parent="http://x/", parent_status=200),
    )
    llm = ScriptedLlm("[0]")
    mw = llm_frontier(llm, "board members", fields=["name", "role"])

    async def nxt(pending: "tuple[FrontierItem, ...]") -> "list[FrontierItem]":
        return [pending[0]]

    _run(mw(items, nxt))
    assert "Board of Directors" in llm.prompt  # link text
    assert "flags: record_list" in llm.prompt and "status 200" in llm.prompt  # parent assessment
    assert "name, role" in llm.prompt  # the schema/fields


def test_llm_frontier_falls_back_to_fifo_on_bad_reply() -> None:
    from web.crawl import FrontierItem

    from web.onboard import llm_frontier

    items = tuple(FrontierItem(url=f"http://x/{i}") for i in range(3))
    mw = llm_frontier(ScriptedLlm("sorry, no idea"), "goal")  # no JSON array -> fall back

    async def nxt(pending: "tuple[FrontierItem, ...]") -> "list[FrontierItem]":
        return [pending[0]]

    picked = cast("list[FrontierItem]", _run(mw(items, nxt)))
    assert [it.url for it in picked] == ["http://x/0"]  # FIFO


def test_cli_author_shim_writes_query(
    httpserver: HTTPServer,
    capsys: "pytest.CaptureFixture[str]",
    monkeypatch: "pytest.MonkeyPatch",
    tmp_path: object,
) -> None:
    # --shim routes author through ClaudeShim (the local claude -p) instead of the API client.
    httpserver.expect_request("/people").respond_with_data(_PEOPLE, content_type="text/html")
    brief = _brief_file(tmp_path, httpserver.url_for("/people"))
    monkeypatch.setattr("web.onboard.__main__.ClaudeShim", lambda **_k: _ClosableLlm(_REPLY))
    rc = main(["author", brief, "--shim", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 0
    Plan.from_blob(out.out.strip())  # the shim wrote a rebuildable wq blob
    assert "query:" in out.err


def test_cli_locate_then_author_chain_via_cache(
    httpserver: HTTPServer,
    capsys: "pytest.CaptureFixture[str]",
    monkeypatch: "pytest.MonkeyPatch",
    tmp_path: object,
) -> None:
    # the run-separately chain: locate caches the Reference; author picks it up (no piping, no --ref).
    from pathlib import Path

    httpserver.expect_request("/people").respond_with_data(_PEOPLE, content_type="text/html")
    monkeypatch.setenv("XDG_CACHE_HOME", str(Path(str(tmp_path)) / "cache"))
    brief = _brief_file(tmp_path, httpserver.url_for("/people"))

    assert main(["locate", brief]) == 0  # writes the cache
    assert "cached →" in capsys.readouterr().err

    monkeypatch.setattr("web.onboard.__main__.AnthropicLlm", lambda **_k: _ClosableLlm(_REPLY))
    assert main(["author", brief]) == 0  # reads the cached reference
    out = capsys.readouterr()
    assert "using the located reference" in out.err and Plan.from_blob(out.out.strip())


def test_cli_locate_pipes_into_author(
    httpserver: HTTPServer,
    capsys: "pytest.CaptureFixture[str]",
    monkeypatch: "pytest.MonkeyPatch",
    tmp_path: object,
) -> None:
    # the pipe form: locate's stdout (a Reference JSON) is exactly what `author --ref -` consumes.
    httpserver.expect_request("/people").respond_with_data(_PEOPLE, content_type="text/html")
    brief = _brief_file(tmp_path, httpserver.url_for("/people"))
    rc = main(["locate", brief, "--no-cache"])
    located = capsys.readouterr().out.strip()
    assert rc == 0
    monkeypatch.setattr("sys.stdin", type("S", (), {"read": staticmethod(lambda: located)})())
    monkeypatch.setattr("web.onboard.__main__.AnthropicLlm", lambda **_k: _ClosableLlm(_REPLY))
    rc = main(["author", brief, "--ref", "-", "--no-cache"])
    assert rc == 0 and Plan.from_blob(capsys.readouterr().out.strip())


def test_llm_pricing_meters_spend_from_usage() -> None:
    # $3/M input, $15/M output, $0.30/M cache-read, $3.75/M cache-write (a Sonnet-like schedule)
    pricing = Pricing(input=3.0, output=15.0, cache_read=0.30, cache_write=3.75)
    total = Usage(input=1000, output=500) + Usage(input=200, cache_read=4000)
    assert total.input == 1200 and total.cache_read == 4000
    cost = pricing.cost(total)
    assert abs(cost - (1200 * 3.0 + 500 * 15.0 + 4000 * 0.30) / 1_000_000) < 1e-12


def test_packaged_briefs_are_available_and_loadable() -> None:
    from web.onboard.__main__ import _load_brief, _packaged_briefs

    assert {"news", "products", "people"} <= set(_packaged_briefs())
    brief = _load_brief("news")  # by packaged name -> loads its frontmatter
    assert brief.name == "news" and "headline" in brief.fields and brief.search


def test_ddg_search_parses_result_urls(monkeypatch: "pytest.MonkeyPatch") -> None:
    import ddgs

    from web.onboard import DdgSearch

    class _FakeDDGS:
        def __enter__(self) -> "_FakeDDGS":
            return self

        def __exit__(self, *_a: object) -> bool:
            return False

        def text(self, query: str, max_results: int, backend: str) -> list[dict[str, str]]:
            return [
                {"href": "https://a.com", "title": "A"},
                {"url": "https://b.com"},
                {"title": "no url here"},
                {"href": "https://a.com"},  # dupe dropped
            ]

    monkeypatch.setattr(ddgs, "DDGS", _FakeDDGS)
    urls = cast("list[str]", _run(DdgSearch()("latest news")))
    assert urls == ["https://a.com", "https://b.com"]
