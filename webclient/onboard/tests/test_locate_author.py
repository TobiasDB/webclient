"""Locate + Author -- the reusable phases on the wq DSL.

Covers: Locate selecting a dataset candidate; Locate PREFERRING a consistent XHR/data-API JSON
endpoint over the page; Author driving a (scripted) LLM over the patterns guide to write a WORKING
wq query for an HTML repeating-record list, a JSON data-API, and an HTML header table; and the
composition end to end. The LLM is a scripted stub -- so these test the pipeline MECHANICS (prompt
-> parse -> reroot -> run), not a model's selector quality.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import cast

import pytest
from pytest_httpserver import HTTPServer
from web.dsl import Plan
from web.onboard import (
    Brief,
    DatasetBrief,
    LocateBrief,
    Pricing,
    Reference,
    Usage,
    build_query,
    locate_and_author,
)
from web.onboard.__main__ import main
from web.onboard.author import author  # the reference-based one-shot primitive
from web.onboard.locate import locate  # the core (explicit resolver/search/review)
from web.resolve import Resolver


def _stage_reply(prompt: str) -> "str | None":
    """The canned answer for a LOCATE stage prompt -- so a scripted model plays the pipeline's
    stages offline: VERIFY keeps every search result, SELECT marks every listed page a ``must``,
    EVALUATE says the dataset is present + scrapable, and the author loop's YES/NO checks accept.
    ``None`` for any other prompt (the author's query request -> the scripted reply)."""
    if "Which results clearly belong to" in prompt:  # search: verify_seeds
        n = len(re.findall(r"^\d+\. ", prompt, flags=re.M))
        return json.dumps({"belong": list(range(n)), "note": "all"})
    if "Here are the crawled pages" in prompt:  # select_candidates (metadata only)
        urls = re.findall(r'"url": "([^"]+)"', prompt)
        return json.dumps(
            [{"url": u, "kind": "page", "tier": "must", "reason": "the dataset"} for u in urls]
        )
    if "Assess this page as the source to scrape" in prompt:  # evaluate_candidate
        return json.dumps(
            {
                "dataset_present": True,
                "is_queryable": False,
                "completeness": "full",
                "scrapability": 9,
                "verdict": "the dataset listing",
            }
        )
    if "Answer YES or NO" in prompt:  # the author loop's check / review
        return "YES — the page holds the requested dataset."
    return None


class ScriptedLlm:
    """An :class:`~web.onboard.Llm` that returns one canned ``wq`` reply, and records the prompt it
    was given -- a deterministic stand-in for a real model so Author is testable offline.
    """

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompt = ""

    async def complete(self, prompt: str) -> str:
        self.prompt = prompt
        staged = _stage_reply(prompt)
        if staged is not None:  # a locate stage (verify / select / evaluate) or a YES/NO check
            return staged
        return self.reply


def _run(coro: object) -> object:
    return asyncio.run(cast("asyncio.Future[object]", coro))


@pytest.fixture(autouse=True)
def _stub_locate_render(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Locate renders the WINNING candidate in a real browser to detect JS-gating (static vs
    rendered record count). A unit test must not launch a browser, so stub the render to fetch the
    page over plain HTTP on the shared pool: a static test page renders to the SAME content, so
    Locate correctly bakes ``basic``. A JS-gating test overrides this to return a richer snapshot.
    """
    import importlib

    from web.fetch import Request as _Req
    from web.resolve import Resolver as _R
    from web.resolve import profiles as _rp

    async def _http_render(url: str, pool: object) -> object:
        return await _R(profile=_rp.BASIC, pool=cast("Any", pool)).snapshot(_Req(url=url))

    # patch on the MODULE object: the package re-exports the `locate` FUNCTION, which shadows the
    # submodule under any attribute access (`web.onboard.locate` -> the function), so importlib is the
    # only way to reach the real module to set its `_render_page`.
    monkeypatch.setattr(importlib.import_module("web.onboard.locate"), "_render_page", _http_render)


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


_ACME_EVENTS = (
    b"<html><body><h1>Acme Corp Investor Events</h1><ul>"
    b"<li class='row'><span class='name'>Q1 2025 Earnings Call</span></li>"
    b"<li class='row'><span class='name'>Q2 2025 Earnings Call</span></li>"
    b"<li class='row'><span class='name'>Annual Meeting</span></li>"
    b"<li class='row'><span class='name'>Investor Day</span></li>"
    b"</ul></body></html>"
)


class _VerdictLlm:
    """A model stand-in that answers the EVALUATE stage with canned verdicts in sequence
    (best-first) -- so a test drives the model's SELECTION: the pipeline leaves the "does this page
    hold the entity's dataset" call to the model, not a hardcoded rule. Plays the other stages
    (select / verify) straight. Records each prompt it saw."""

    def __init__(self, verdicts: "list[bool]") -> None:
        self._verdicts = verdicts
        self.prompts: "list[str]" = []
        self._evaluated = 0

    async def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if "Assess this page as the source to scrape" in prompt:
            ok = self._verdicts[min(self._evaluated, len(self._verdicts) - 1)]
            self._evaluated += 1
            return json.dumps(
                {
                    "dataset_present": ok,
                    "scrapability": 9 if ok else 0,
                    "verdict": "holds the dataset" if ok else "a third party's page ABOUT it",
                }
            )
        return _stage_reply(prompt) or "[]"


def test_locate_evaluate_can_veto_the_only_candidate(httpserver: HTTPServer) -> None:
    # the EVALUATE stage judges the candidate; a "not present" verdict FAILS Locate even for a
    # record-list page. The model -- not a rule -- decides whether the source holds the entity's data.
    httpserver.expect_request("/events").respond_with_data(_ACME_EVENTS, content_type="text/html")

    async def go(ok: bool) -> "tuple[Reference | None, _VerdictLlm]":
        llm = _VerdictLlm([ok])
        async with Resolver() as r:
            ref = await locate(
                LocateBrief(goal="investor events", candidates=[httpserver.url_for("/events")]),
                resolver=r,
                entity="Acme Corp",
                review=llm,
            )
            return ref, llm

    ref_yes, llm_yes = cast("tuple[Reference | None, _VerdictLlm]", _run(go(True)))
    assert ref_yes is not None
    evaluate = [p for p in llm_yes.prompts if "Assess this page" in p]
    assert evaluate and "Acme Corp" in evaluate[0]  # the entity is handed to the model as context
    # what the model SEES per stage: evaluate gets the flags + ONE (clipped) skeleton ...
    assert "Detected flags" in evaluate[0] and "Page skeleton" in evaluate[0]
    select = [p for p in llm_yes.prompts if "Here are the crawled pages" in p]
    assert select and "Page skeleton" not in select[0]  # ... select saw METADATA only
    ref_no, _ = cast("tuple[Reference | None, _VerdictLlm]", _run(go(False)))
    assert ref_no is None  # model vetoed -> Locate fails (allowed)


def test_locate_seeds_from_search_results_not_an_llm_guess(httpserver: HTTPServer) -> None:
    # seeds come from SEARCH (real DdgSearch result URLs), NOT an LLM-guessed url (which hallucinates
    # a 404). locate crawls the seed the search returned and returns it when it holds the dataset --
    # and never prompts the model to GUESS a URL.
    httpserver.expect_request("/events").respond_with_data(_ACME_EVENTS, content_type="text/html")
    official = httpserver.url_for("/events")
    prompts: "list[str]" = []

    class _Stub:  # only the YES/NO candidate review now -- there is no URL-guessing step
        async def complete(self, prompt: str) -> str:
            prompts.append(prompt)
            return _stage_reply(prompt) or "[]"

    async def go() -> "Reference | None":
        async def search(_q: str) -> "list[str]":
            return [official]  # the search backend supplies the seed URL

        async with Resolver() as r:
            return await locate(
                LocateBrief(goal="investor events"),
                resolver=r,
                search=search,
                entity="Acme Corp",
                review=cast("object", _Stub()),
            )

    ref = cast("Reference | None", _run(go()))
    assert ref is not None and ref.url == official  # the searched seed won
    assert not any(
        "URL" in p and "guess" in p.lower() for p in prompts
    )  # never asked to guess a URL


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


def test_locate_stamps_the_working_transport_profile(httpserver: HTTPServer) -> None:
    # a static page is fetched over HTTP -> the reference carries profile "basic" (no browser).
    httpserver.expect_request("/people").respond_with_data(_PEOPLE, content_type="text/html")

    async def go() -> "Reference | None":
        async with Resolver() as r:
            return await locate(
                LocateBrief(goal="team", candidates=[httpserver.url_for("/people")]), resolver=r
            )

    ref = cast("Reference | None", _run(go()))
    assert ref is not None and ref.profile == "basic"


def test_locate_bakes_a_browser_profile_for_a_js_gated_page(
    httpserver: HTTPServer, monkeypatch: "pytest.MonkeyPatch"
) -> None:
    # a server-rendered SHELL carries only a couple of teaser rows; the real list is injected by JS.
    # Rendering reveals materially more records, so Locate must bake `full_browser`, not `basic`.
    shell = (
        b"<html><body><main><ul>"
        + b"".join(
            b"<li class='row'><span class='name'>Teaser %d</span></li>" % i for i in range(3)
        )
        + b"</ul></main></body></html>"
    )  # 3 teaser rows -- enough to be a candidate, but the real list is bigger and JS-injected
    httpserver.expect_request("/js").respond_with_data(shell, content_type="text/html")
    rendered = (
        b"<html><body><main><ul>"
        + b"".join(
            b"<li class='row'><span class='name'>Person %d</span></li>" % i for i in range(10)
        )
        + b"</ul></main></body></html>"
    )

    import importlib

    from web.fetch import Request as _Req
    from web.fetch import Snapshot as _Snap

    async def _render(url: str, pool: object) -> _Snap:
        return _Snap(request=_Req(url=url), url=url, status=200, content=rendered)

    # reveals 8 rows vs 2 static; patch the real module (see the autouse fixture's note on shadowing)
    monkeypatch.setattr(importlib.import_module("web.onboard.locate"), "_render_page", _render)

    async def go() -> "Reference | None":
        async with Resolver() as r:
            return await locate(
                LocateBrief(goal="people", fields=["name"], candidates=[httpserver.url_for("/js")]),
                resolver=r,
            )

    ref = cast("Reference | None", _run(go()))
    assert ref is not None and ref.profile == "full_browser" and ref.needs_browser


def test_author_bakes_the_working_profile_into_the_query(httpserver: HTTPServer) -> None:
    # the reference's working profile is baked into the query root (resolve(profile=...)), so it uses
    # the known transport instead of re-running resolve escalation.
    httpserver.expect_request("/people").respond_with_data(_PEOPLE, content_type="text/html")
    ref = Reference(
        url=httpserver.url_for("/people"), kind="html", record_selector="li.row", profile="basic"
    )
    llm = ScriptedLlm(
        'wq.doc.select_all("li.row").extract(name=wq.doc.select(".name").attr("text"))'
    )

    async def go() -> str:
        async with Resolver() as r:
            q, _engine, _notes = await build_query(
                ref, DatasetBrief(fields=["name"]), resolver=r, llm=llm
            )
            return q.to_blob()

    blob = cast("str", _run(go()))
    assert '"profile"' in blob and "basic" in blob  # the profile is baked into the resolve step


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


_NAME_ONLY = 'wq.doc.select_all("li.row").extract(name=wq.doc.select(".name").attr("text"))'
_NAME_ROLE = (
    'wq.doc.select_all("li.row").extract('
    'name=wq.doc.select(".name").attr("text"), role=wq.doc.select(".role").attr("text"))'
)


def _rerooted(chain: str, url: str) -> object:
    from web.onboard.compile import parse_query, reroot

    return reroot(parse_query(chain), url)


class _SeqLlm:
    """Returns canned replies in sequence (the agent loop makes several distinct author calls)."""

    def __init__(self, replies: "list[str]") -> None:
        self._replies = replies
        self.i = 0

    async def complete(self, prompt: str) -> str:
        reply = self._replies[min(self.i, len(self._replies) - 1)]
        self.i += 1
        return reply


_LISTING = (
    b"<html><body><ul>"
    b"<li class='row'><span class='name'>A</span><a class='more' href='/detail/1'>read</a></li>"
    b"<li class='row'><span class='name'>B</span><a class='more' href='/detail/2'>read</a></li>"
    b"</ul></body></html>"
)


def test_author_agent_nests_a_detail_extraction(httpserver: HTTPServer) -> None:
    # the agent loop: base query (name + link), then -- body missing, a record link exists -- a
    # detail turn that resolves each record's link and extracts the body from the detail page.
    from web.onboard import author_agent

    httpserver.expect_request("/list").respond_with_data(_LISTING, content_type="text/html")
    httpserver.expect_request("/detail/1").respond_with_data(
        b"<article class='body'>Body One</article>", content_type="text/html"
    )
    httpserver.expect_request("/detail/2").respond_with_data(
        b"<article class='body'>Body Two</article>", content_type="text/html"
    )
    base = (
        'wq.doc.select_all("li.row").extract('
        'name=wq.doc.select(".name").attr("text"), url=wq.doc.select("a.more").attr("href"))'
    )
    detail = (
        'wq.doc.select_all("li.row").extract('
        'name=wq.doc.select(".name").attr("text"), url=wq.doc.select("a.more").attr("href"), '
        'body=wq.doc.select("a.more").attr("href").resolve().select("article.body").attr("text"))'
    )
    llm = _SeqLlm([base, detail])

    async def go() -> object:
        async with Resolver() as r:
            queries, verdict = await author_agent(
                Reference(url=httpserver.url_for("/list"), kind="html"),
                DatasetBrief(fields=["name", "url", "body"]),
                resolver=r,
                llm=cast("object", llm),
            )
            assert verdict.ok  # the loop reached done
            assert queries
            q = queries[0]
            return await q.acollect(resolver=r)

    rows = cast("list[dict[str, object]]", _run(go()))
    assert any(row.get("body") == "Body One" for row in rows)  # the detail turn nested the body


def test_author_agent_repairs_a_failed_query(httpserver: HTTPServer) -> None:
    # the repair edge: the base query first (a) does not parse, then (b) matches 0 records, then
    # (c) is correct -- the loop feeds each failure back and re-authors, reaching rows.
    from web.onboard import author_agent

    httpserver.expect_request("/list").respond_with_data(_LISTING, content_type="text/html")
    bad_parse = 'wq.doc.select_all("li.row").extract(name={"x": 1})'  # disallowed Dict -> reject
    zero_rows = 'wq.doc.select_all(".nope").extract(name=wq.doc.select(".name").attr("text"))'
    good = 'wq.doc.select_all("li.row").extract(name=wq.doc.select(".name").attr("text"))'
    llm = _SeqLlm([bad_parse, zero_rows, good])

    async def go() -> object:
        async with Resolver() as r:
            queries, verdict = await author_agent(
                Reference(url=httpserver.url_for("/list"), kind="html"),
                DatasetBrief(fields=["name"]),
                resolver=r,
                llm=cast("object", llm),
            )
            assert verdict.ok  # the loop repaired its way to done
            assert queries
            q = queries[0]
            return await q.acollect(resolver=r)

    rows = cast("list[dict[str, object]]", _run(go()))
    assert [row.get("name") for row in rows] == ["A", "B"]  # repaired past parse-reject + 0 rows
    assert llm.i >= 3  # it took the two repair turns (base + repair + repair)


def test_author_agent_splits_across_a_sibling_page(httpserver: HTTPServer) -> None:
    # SPLIT-SOURCE: the dataset spans two pages. Base authors /past; the review flags it incomplete;
    # the model replies SIBLING: /upcoming; the loop authors that as a 2nd section; the CLI/run
    # concatenates. author_agent returns BOTH section queries.
    from web.onboard import author_agent

    past = (
        b"<html><body><ul>"
        b"<li class='row'><span class='name'>Q1 Call (past)</span></li>"
        b"<li class='row'><span class='name'>Q2 Call (past)</span></li>"
        b"</ul></body></html>"
    )
    upcoming = (
        b"<html><body><ul>"
        b"<li class='row'><span class='name'>Q3 Call (upcoming)</span></li>"
        b"</ul></body></html>"
    )
    httpserver.expect_request("/past").respond_with_data(past, content_type="text/html")
    httpserver.expect_request("/upcoming").respond_with_data(upcoming, content_type="text/html")
    up_url = httpserver.url_for("/upcoming")
    extract = 'wq.doc.select_all("li.row").extract(name=wq.doc.select(".name").attr("text"))'
    author = _SeqLlm([extract, f"SIBLING: {up_url}", extract])
    # check(past)=YES, review(past)=NO(missing upcoming), check(upcoming)=YES, review(upcoming)=YES
    review = _SeqLlm(["YES", "NO — the upcoming events are missing (separate page)", "YES", "YES"])

    async def go() -> "tuple[list[object], int]":
        async with Resolver() as r:
            queries, verdict = await author_agent(
                Reference(url=httpserver.url_for("/past"), kind="html"),
                DatasetBrief(fields=["name"], review_hint="require upcoming AND past"),
                resolver=r,
                llm=cast("object", author),
                review=cast("object", review),
                entity="Acme",
            )
            assert verdict.ok and len(queries) == 2  # two section queries (past + upcoming)
            rows: "list[object]" = []
            for q in queries:
                rows.extend(await q.acollect(resolver=r))
            return rows, len(queries)

    rows, n = cast("tuple[list[dict[str, object]], int]", _run(go()))
    names = [row.get("name") for row in rows]
    assert n == 2
    assert "Q1 Call (past)" in names and "Q3 Call (upcoming)" in names  # both sections concatenated


def test_author_agent_check_is_advisory_not_fatal(httpserver: HTTPServer) -> None:
    # the entry check is ADVISORY: even when it says NO, the loop still AUTHORS (a skeleton read is
    # unreliable -- it wrongly rejected pages that extract fine). Absence is concluded empirically.
    from web.onboard import author_agent

    httpserver.expect_request("/list").respond_with_data(_LISTING, content_type="text/html")
    good = 'wq.doc.select_all("li.row").extract(name=wq.doc.select(".name").attr("text"))'
    author = _SeqLlm([good])
    review = _SeqLlm(["NO — the data does not look present here", "YES the rows are correct"])

    async def go() -> object:
        async with Resolver() as r:
            queries, verdict = await author_agent(
                Reference(url=httpserver.url_for("/list"), kind="html"),
                DatasetBrief(fields=["name"]),
                resolver=r,
                llm=cast("object", author),
                review=cast("object", review),
                entity="Acme",
            )
            assert verdict.ok and queries  # authored despite the check's NO
            q = queries[0]
            return await q.acollect(resolver=r)

    rows = cast("list[dict[str, object]]", _run(go()))
    assert [row.get("name") for row in rows] == ["A", "B"]  # the advisory check did not veto it
    assert author.i == 1  # the author ran (was not skipped)


def test_author_agent_threads_brief_hints_and_review_guidance(httpserver: HTTPServer) -> None:
    # per-stage NL guidance from the brief reaches the right stage: `hints` -> the author prompt,
    # `review` -> the sample-review prompt (brief-specific strictness, not hardcoded in the pipeline).
    from web.onboard import author_agent

    httpserver.expect_request("/list").respond_with_data(_LISTING, content_type="text/html")
    good = 'wq.doc.select_all("li.row").extract(name=wq.doc.select(".name").attr("text"))'

    class _Rec:  # records every prompt it sees
        def __init__(self, replies: "list[str]") -> None:
            self.replies, self.i, self.prompts = replies, 0, []  # type: ignore[var-annotated]

        async def complete(self, prompt: str) -> str:
            self.prompts.append(prompt)
            r = self.replies[min(self.i, len(self.replies) - 1)]
            self.i += 1
            return r

    author, review = _Rec([good]), _Rec(["YES present", "YES the rows are good"])
    brief = DatasetBrief(
        fields=["name"], author_hint="EVENTS-HINT-TOKEN", review_hint="TIMELINESS-REVIEW-TOKEN"
    )

    async def go() -> None:
        async with Resolver() as r:
            await author_agent(
                Reference(url=httpserver.url_for("/list"), kind="html"),
                brief,
                resolver=r,
                llm=cast("object", author),
                review=cast("object", review),
                entity="Acme",
            )

    _run(go())
    assert any("EVENTS-HINT-TOKEN" in p for p in author.prompts)  # author_hint -> author
    assert any("TIMELINESS-REVIEW-TOKEN" in p for p in review.prompts)  # review_hint -> review
    # review_hint is a REQUIREMENT: the author must know it up front, not just be judged on it
    assert any("TIMELINESS-REVIEW-TOKEN" in p for p in author.prompts)


def test_author_agent_review_drives_a_repair(httpserver: HTTPServer) -> None:
    # per-stage review: the first sample is rejected (incomplete), so the loop repairs and re-authors
    # until the reviewer accepts.
    from web.onboard import author_agent

    httpserver.expect_request("/list").respond_with_data(_LISTING, content_type="text/html")
    q1 = 'wq.doc.select_all("li.row").extract(name=wq.doc.select(".name").attr("text"))'
    q2 = 'wq.doc.select_all("li.row").extract(name=wq.doc.select(".name").attr("text"), status="X")'
    author = _SeqLlm([q1, q2])
    review = _SeqLlm(
        ["YES the data is here", "NO — the archived events are missing", "YES complete"]
    )

    async def go() -> object:
        async with Resolver() as r:
            queries, verdict = await author_agent(
                Reference(url=httpserver.url_for("/list"), kind="html"),
                DatasetBrief(fields=["name"]),
                resolver=r,
                llm=cast("object", author),
                review=cast("object", review),
                entity="Acme",
            )
            assert verdict.ok and queries
            q = queries[0]
            return await q.acollect(resolver=r)

    rows = cast("list[dict[str, object]]", _run(go()))
    assert [row.get("name") for row in rows] == ["A", "B"]
    assert author.i == 2 and review.i >= 3  # base + one review-driven repair


def test_run_to_sink_routes_rows_to_table_and_documents_to_store(httpserver: HTTPServer) -> None:
    from web.onboard import MemorySink, run_to_sink

    listing = (
        b"<html><body><ul>"
        b"<li class='row'><span class='title'>Report A</span><a class='file' href='/files/a.txt'>dl</a></li>"
        b"<li class='row'><span class='title'>Report B</span><a class='file' href='/files/b.txt'>dl</a></li>"
        b"</ul></body></html>"
    )
    httpserver.expect_request("/docs").respond_with_data(listing, content_type="text/html")
    httpserver.expect_request("/files/a.txt").respond_with_data(
        b"BODY-A", content_type="text/plain"
    )
    httpserver.expect_request("/files/b.txt").respond_with_data(
        b"BODY-B", content_type="text/plain"
    )
    chain = (
        'wq.doc.select_all("li.row").extract('
        'title=wq.doc.select(".title").attr("text"), file=wq.doc.select("a.file").attr("href"))'
    )
    brief = DatasetBrief(fields=["title", "file"], types={"file": "document"})  # file is a blob

    async def go() -> MemorySink:
        async with Resolver() as r:
            q = cast("object", _rerooted(chain, httpserver.url_for("/docs")))
            sink = MemorySink()
            rows, blobs = await run_to_sink(cast("object", q), brief, sink, resolver=r)
            assert (rows, blobs) == (2, 2)
            return sink

    sink = cast("MemorySink", _run(go()))
    # scalar rows -> the table (the document field is NOT a table column)
    assert {"title": "Report A"} in sink.rows and all("file" not in row for row in sink.rows)
    # documents -> the object store, each keyed by its URL + carrying its row's metadata
    a_url = httpserver.url_for("/files/a.txt")
    assert sink.blobs[a_url][0] == b"BODY-A" and sink.blobs[a_url][1] == {"title": "Report A"}


def test_has_records_requires_schema_corroboration() -> None:
    # a repeating region ALONE is a weak "the data is here" signal (nav/news lists match too), so
    # when the brief names fields it must be corroborated -- else Locate renders to check (a JS-gated
    # shell whose only static lists are chrome must not read as "present").
    from web.onboard.locate import _has_records
    from web.parse import parse

    events = parse(
        b"<ul>"
        b"<li class='row'><span>Q1 Earnings Call</span><span>webcast 2025-01-01</span></li>"
        b"<li class='row'><span>Q2 Earnings Call</span><span>webcast 2025-04-01</span></li>"
        b"</ul>",
        content_type="text/html",
    )
    nav = parse(
        b"<ul><li class='row'><a>Home</a></li><li class='row'><a>About</a></li>"
        b"<li class='row'><a>Investors</a></li></ul>",
        content_type="text/html",
    )
    brief = DatasetBrief(fields=["title", "webcast", "datetime"])
    assert _has_records(events, brief)  # 'webcast' is shown -> corroborated
    assert not _has_records(
        nav, brief
    )  # a chrome list that shows none of the fields -> not present
    assert _has_records(nav, DatasetBrief())  # no fields to corroborate -> the region is the signal


def test_ignored_hard_filters_brief_forbidden_hosts() -> None:
    # a brief's `ignore` entries HARD-exclude their hosts, so a forbidden aggregator never wins even
    # as the only survivor when the real source 404s (Locate then fails, which is correct).
    from web.onboard.evaluate import ignored

    ignore = ["third-party aggregators", "Benzinga", "MarketScreener", "Yahoo Finance"]
    assert ignored("https://www.benzinga.com/quote/AMD", ignore)
    assert ignored("https://finance.yahoo.com/quote/AMD", ignore)  # a two-word host token matches
    assert not ignored("https://investors.amd.com/events", ignore)  # the entity's OWN IR host kept
    assert not ignored("https://ir.example.com/calendar", ignore)  # no ignore token in the host


def test_run_returns_a_dataset_of_rows_and_documents(httpserver: HTTPServer) -> None:
    # the clean execute interface: run(query, brief=...) -> Dataset(rows=[...], documents=[...]).
    from web.onboard import Dataset, run

    listing = (
        b"<html><body><ul>"
        b"<li class='row'><span class='title'>Report A</span><a class='file' href='/files/a.txt'>dl</a></li>"
        b"<li class='row'><span class='title'>Report B</span><a class='file' href='/files/b.txt'>dl</a></li>"
        b"</ul></body></html>"
    )
    httpserver.expect_request("/docs").respond_with_data(listing, content_type="text/html")
    httpserver.expect_request("/files/a.txt").respond_with_data(
        b"BODY-A", content_type="text/plain"
    )
    httpserver.expect_request("/files/b.txt").respond_with_data(
        b"BODY-B", content_type="text/plain"
    )
    chain = (
        'wq.doc.select_all("li.row").extract('
        'title=wq.doc.select(".title").attr("text"), file=wq.doc.select("a.file").attr("href"))'
    )
    brief = DatasetBrief(fields=["title", "file"], types={"file": "document"})

    async def go() -> "Dataset | None":
        async with Resolver() as r:
            q = _rerooted(chain, httpserver.url_for("/docs"))
            return await run([cast("object", q)], resolver=r, brief=brief)

    data = cast("Dataset", _run(go()))
    assert isinstance(data, Dataset)
    assert [row["title"] for row in data.rows] == ["Report A", "Report B"]  # the scalar table
    assert sorted(a.content for a in data.documents) == [b"BODY-A", b"BODY-B"]  # fetched documents
    assert data.documents[0].metadata.get("title") in ("Report A", "Report B")  # keyed to its row


def test_guide_for_selects_examples_by_kind_and_situation() -> None:
    from web.onboard.patterns import guide_for

    html = guide_for([], "html")
    assert "flat HTML list" in html and "JSON / API document" not in html  # html -> list example
    js = guide_for([], "json")
    assert "JSON / API document" in js and "flat HTML list" not in js  # json -> the JSON example
    assert "DETAIL page" in guide_for(
        [], "html", detail=True
    )  # detail turn -> the follow-a-link ex
    assert "Writing a `wq`" in html  # the preamble (core syntax) is always included


def test_review_revises_the_query_to_add_a_missing_field(httpserver: HTTPServer) -> None:
    from web.onboard import review
    from web.onboard.compile import Query

    httpserver.expect_request("/people").respond_with_data(_PEOPLE, content_type="text/html")
    url = httpserver.url_for("/people")
    initial = cast("Query", _rerooted(_NAME_ONLY, url))  # extracts name only
    llm = ScriptedLlm(_NAME_ROLE)  # the reviewer proposes a query that also gets role

    async def go() -> object:
        async with Resolver() as r:
            q, notes = await review(
                initial,
                Reference(url=url),
                DatasetBrief(fields=["name", "role"]),
                resolver=r,
                llm=llm,
                rounds=1,
            )
            assert notes  # the query was revised
            return await q.acollect(resolver=r)

    rows = cast("list[dict[str, object]]", _run(go()))
    assert {"name": "Alice", "role": "CEO"} in rows  # the revised query now extracts role


def test_review_keeps_the_query_when_the_model_says_done(httpserver: HTTPServer) -> None:
    from web.onboard import review
    from web.onboard.compile import Query

    httpserver.expect_request("/people").respond_with_data(_PEOPLE, content_type="text/html")
    url = httpserver.url_for("/people")
    initial = cast("Query", _rerooted(_NAME_ONLY, url))
    llm = ScriptedLlm("DONE")  # the reviewer is satisfied

    async def go() -> "list[str]":
        async with Resolver() as r:
            q, notes = await review(
                initial, Reference(url=url), DatasetBrief(fields=["name"]), resolver=r, llm=llm
            )
            return notes

    assert cast("list[str]", _run(go())) == []  # unchanged


def test_brief_schema_parses_type_and_description() -> None:
    text = (
        "---\n"
        "name: news\n"
        "schema:\n"
        "  - headline: {type: string, description: the article headline}\n"
        "  - published: {type: datetime, description: the publish time}\n"
        "  - url\n"
        "---\n"
        "news\n"
    )
    brief = Brief.from_markdown(text)
    assert brief.fields == ["headline", "published", "url"]
    assert brief.types == {"headline": "string", "published": "datetime"}  # {type,description} form
    assert brief.descriptions == {
        "headline": "the article headline",
        "published": "the publish time",
    }


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


def test_llm_frontier_caps_the_prompt_to_a_keyword_ranked_window() -> None:
    # the crawl frontier GROWS every round; sending all of it is the runaway LLM token cost. The
    # model is shown only a bounded, keyword-ranked window -- so the prompt stays small AND the
    # relevant edge (buried among hundreds) is surfaced into it.
    from web.crawl import FrontierItem
    from web.onboard import llm_frontier
    from web.onboard.frontier import _FRONTIER_WINDOW, _prompt, _window

    pending = [FrontierItem(url=f"http://x/junk/{i}", text="misc") for i in range(300)]
    pending.append(FrontierItem(url="http://x/investors/events", text="Events Calendar earnings"))
    win = _window(pending, "earnings events", ["date"], ["events calendar"], [])
    assert len(win) == _FRONTIER_WINDOW < len(pending)  # bounded
    assert any("investors/events" in it.url for it in win)  # the relevant edge made the window
    # the prompt lists only the window, so its size does not grow with the frontier
    assert (
        _prompt("earnings events", ["date"], [], [], win, 5).count("http://x/") <= _FRONTIER_WINDOW
    )

    captured: "list[str]" = []

    class _Spy:
        async def complete(self, prompt: str) -> str:
            captured.append(prompt)
            return '[{"n": 0, "why": "top-ranked"}]'

    mw = llm_frontier(_Spy(), "earnings events", look=["events calendar"])

    async def nxt(p: "tuple[FrontierItem, ...]") -> "list[FrontierItem]":
        return list(p)

    picked = cast("list[FrontierItem]", _run(mw(tuple(pending), nxt)))
    assert captured and captured[0].count("http://x/") <= _FRONTIER_WINDOW  # bounded prompt sent
    assert "investors/events" in picked[0].url  # index 0 maps into the WINDOW, not raw pending


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


def test_llm_frontier_emits_reasoning() -> None:
    from web.crawl import FrontierItem
    from web.fetch import Trace
    from web.onboard import ReasonEvent, llm_frontier

    items = (FrontierItem(url="http://x/board"), FrontierItem(url="http://x/careers"))
    mw = llm_frontier(ScriptedLlm('[{"n": 0, "why": "the board listing"}]'), "board members")

    async def nxt(pending: "tuple[FrontierItem, ...]") -> "list[FrontierItem]":
        return [pending[0]]

    with Trace() as t:
        picked = cast("list[FrontierItem]", _run(mw(items, nxt)))
    assert [it.url for it in picked] == ["http://x/board"]
    reasons = [e for e in t.events if isinstance(e, ReasonEvent) and e.stage == "frontier"]
    assert (
        reasons
        and reasons[0].text == "the board listing"
        and reasons[0].subject == "http://x/board"
    )


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
    from web.onboard import DdgSearch, SearchHit

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
    hits = cast("list[SearchHit]", _run(DdgSearch()("latest news")))
    assert [h.url for h in hits] == ["https://a.com", "https://b.com"]
    assert hits[0].title == "A"  # the title/snippet ride along for the verify filter


def test_author_loop_sends_the_page_once_and_repairs_with_short_follow_ups(
    httpserver: HTTPServer,
) -> None:
    # The authoring loop's memory is a CONVERSATION: the opening (guide + ONE clipped skeleton) is
    # sent exactly once, and a repair is a SHORT follow-up naming the failure -- never a re-send of
    # the page. write_query returns the artifact: the validation verdict + the rejection trail.
    from web.onboard import QueryArtifact, write_query

    httpserver.expect_request("/list").respond_with_data(_LISTING, content_type="text/html")
    zero_rows = 'wq.doc.select_all(".nope").extract(name=wq.doc.select(".name").attr("text"))'
    good = 'wq.doc.select_all("li.row").extract(name=wq.doc.select(".name").attr("text"))'

    class _Conv:  # a Conversational model that records every turn it is sent
        def __init__(self, replies: "list[str]") -> None:
            self.replies, self.turns = replies, []  # type: ignore[var-annotated]

        async def complete(self, prompt: str) -> str:
            return _stage_reply(prompt) or "[]"

        def conversation(self) -> "_Conv":
            return self

        async def send(self, text: str) -> str:
            self.turns.append(text)
            return self.replies[min(len(self.turns) - 1, len(self.replies) - 1)]

    llm = _Conv([zero_rows, good])

    async def go() -> QueryArtifact:
        async with Resolver() as r:
            return await write_query(
                Reference(url=httpserver.url_for("/list"), kind="html"),
                DatasetBrief(fields=["name"]),
                resolver=r,
                llm=cast("object", llm),  # type: ignore[arg-type]
            )

    art = _run(go())
    assert isinstance(art, QueryArtifact)
    assert len(llm.turns) == 2  # the opening, then ONE repair follow-up
    opening, repair = llm.turns
    assert "Base your CSS selectors on this page skeleton" in opening and "li.row" in opening
    assert "li.row" not in repair and len(repair) < len(opening) // 4  # short: no skeleton re-sent
    assert "0 populated rows" in repair  # the ONE-LINE reason the model is told
    assert art.tested and art.complete and art.row_count == 2  # the repaired query extracts
    assert art.attempts and "0 populated rows" in art.attempts[0]  # the rejection trail
    assert art.blob and "li.row" in art.describe and not art.sections  # one section


def test_loading_requirements_bakes_a_browser_for_a_signalled_spa(httpserver: HTTPServer) -> None:
    # Flags are GROUND TRUTH: a page whose own signals say JS-app / needs_browser must bake a browser
    # profile even when the render-count comparison shows no gain -- the "lowest tier that worked"
    # must never win over a detected SPA. A non-signalled page with the same content stays `basic`.
    import importlib

    from web.onboard.evaluate import reference as build_ref
    from web.parse import parse
    from web.resolve import flags

    shell = (  # fires the `spa` SIGNAL (a script bundle + an empty root); no records
        b"<html><head><script src='/static/js/main.3f2a1c.js'></script></head>"
        b"<body><div id='root'></div><noscript>You need to enable JavaScript.</noscript></body></html>"
    )
    httpserver.expect_request("/app").respond_with_data(shell, content_type="text/html")
    httpserver.expect_request("/list").respond_with_data(_LISTING, content_type="text/html")
    loc = importlib.import_module("web.onboard.locate")  # the package re-exports the function
    brief = LocateBrief(goal="people", fields=["name"])

    async def go(html: bytes, path: str) -> Reference:
        page = parse(html, content_type="text/html", url=httpserver.url_for(path))
        ref = build_ref(page, {f.name: f for f in flags(page)})  # carries the page's detections
        async with Resolver() as r:
            return cast(Reference, await loc._loading_requirements(page, ref, r, brief))

    spa = _run(go(shell, "/app"))
    assert (
        spa.profile == "full_browser" and spa.needs_browser
    )  # a detected SPA -> a browser, always
    # a small static list fires only `empty` (the over-firing needs_browser conclusion): with no gain
    # on render it stays HTTP -- the rule keys on the `spa` SIGNAL, not the conclusion.
    assert _run(go(_LISTING, "/list")).profile == "basic"


def test_author_stops_on_a_js_gated_page_without_guessing_selectors(httpserver: HTTPServer) -> None:
    # GROUND TRUTH before any model turn: the fetched page's own signals say JS-app and it holds no
    # record region at the HTTP tier -> a defined `js_gated` stop, ZERO authoring turns (no selector
    # guessing at a shell), and a reason that names the Locate mis-tiering.
    from web.onboard import author_agent, write_query

    shell = (
        b"<html><head><script src='/static/js/main.3f2a1c.js'></script></head>"
        b"<body><div id='root'></div><noscript>You need to enable JavaScript.</noscript></body></html>"
    )
    httpserver.expect_request("/app").respond_with_data(shell, content_type="text/html")
    good = 'wq.doc.select_all("li.row").extract(name=wq.doc.select(".name").attr("text"))'
    llm = _SeqLlm([good])
    ref = Reference(url=httpserver.url_for("/app"), kind="html", profile="basic")

    async def go() -> "tuple[object, object]":
        async with Resolver() as r:
            art = await write_query(ref, DatasetBrief(fields=["name"]), resolver=r, llm=cast("object", llm))  # type: ignore[arg-type]
            queries, _v = await author_agent(ref, DatasetBrief(fields=["name"]), resolver=r, llm=cast("object", llm))  # type: ignore[arg-type]
            return art, queries

    art, queries = _run(go())
    assert art.reason.startswith("js_gated") and "needs_browser" in art.reason  # type: ignore[attr-defined]
    assert not art.blob and not art.tested and art.row_count == 0  # type: ignore[attr-defined]
    assert queries == [] and llm.i == 0  # the model was never asked to write a query


def test_parse_diagnosis_names_the_cause_and_shows_the_reply() -> None:
    # An invalid reply is diagnosed, not just "invalid syntax": WHAT the model replied (a snippet),
    # WHY it failed (the likely mistake), and a hint targeted at that mistake.
    from web.onboard.author_loop import _parse_diagnosis
    from web.onboard.compile import QueryError

    exc = QueryError("source did not parse: invalid syntax")
    cases = {
        "I cannot see any records on this page, sorry.": "prose",
        'wq.doc.select_all("li").extract(name={"x": 1})': "dict literal",
        'wq.doc.select_all("li").extract(name=wq.doc.select(".n").attr("text")': "unbalanced",
        '```python\nwq.doc.select_all("li").extract(name=wq.doc.select(".n").attr("text")).project()\n```': "code fence",
        'wq.reference("http://x").resolve().select_all("li")': "rooted at a reference",
    }
    for reply, expect in cases.items():
        reason, hint = _parse_diagnosis(reply, exc)
        assert expect in reason and "reply began:" in reason and hint, (reply, reason)


# -- the STEP-BY-STEP authoring engine -------------------------------------------------------------


class _StepConv:
    """A Conversational model scripted with one op reply per turn; records every turn it is sent."""

    def __init__(self, replies: "list[str]") -> None:
        self.replies, self.turns = replies, []  # type: ignore[var-annotated]

    async def complete(self, prompt: str) -> str:
        return _stage_reply(prompt) or "[]"

    def conversation(self) -> "_StepConv":
        return self

    async def send(self, text: str) -> str:
        self.turns.append(text)
        return self.replies[min(len(self.turns) - 1, len(self.replies) - 1)]


def _steps_art(httpserver: HTTPServer, llm: _StepConv, fields: "list[str]") -> object:
    from web.onboard import write_query

    async def go() -> object:
        async with Resolver() as r:
            return await write_query(
                Reference(url=httpserver.url_for("/list"), kind="html"),
                DatasetBrief(fields=fields),
                resolver=r,
                llm=cast("object", llm),  # type: ignore[arg-type]
                engine="steps",
            )

    return _run(go())


def test_steps_engine_builds_the_query_one_op_at_a_time(httpserver: HTTPServer) -> None:
    # Each turn the model calls ONE op; the draft is probed against the once-fetched page and the
    # result (matches, the FIRST record's structure, each column's values) comes back with the query
    # so far -- the page skeleton + op menu are sent ONCE in the opening.
    from web.onboard import QueryArtifact

    httpserver.expect_request("/list").respond_with_data(_LISTING, content_type="text/html")
    llm = _StepConv(
        [
            'records("li.row")',
            'field(name, wq.doc.select(".name").attr("text"))',
            'field(url, wq.doc.select("a.more").attr("href"))',
            "done()",
        ]
    )
    art = _steps_art(httpserver, llm, ["name", "url"])
    assert isinstance(art, QueryArtifact)
    assert art.complete and art.row_count == 2, art.reason
    assert len(llm.turns) == 4
    opening, after_records, after_name, after_url = llm.turns
    assert "OPS" in opening and "li.row" in opening and 'records("<css>")' in opening
    assert "matched 2 record(s)" in after_records and "span.name" in after_records
    assert "STILL TO ADD (required): name, url" in after_records
    assert 'name: "A", "B"' in after_name and "QUERY SO FAR" in after_name
    assert "STILL TO ADD (required): url" in after_name
    assert "Every required field is in the query" in after_url
    assert "select_all('li.row')" in art.describe or 'select_all("li.row")' in art.describe
    assert art.sample and art.sample[0] == {"name": "A", "url": httpserver.url_for("/detail/1")}


def test_steps_engine_follows_a_detail_link_once_then_fans_out(httpserver: HTTPServer) -> None:
    # detail(<link css>) fetches the FIRST record's page and shows its structure; detail_field(...)
    # columns then nest under `detail` -- ONE resolve per record, the fan-out inside it.
    from web.onboard import QueryArtifact

    httpserver.expect_request("/list").respond_with_data(_LISTING, content_type="text/html")
    for n, body in ((1, "Body One"), (2, "Body Two")):
        httpserver.expect_request(f"/detail/{n}").respond_with_data(
            f"<article class='body'>{body}</article>".encode(), content_type="text/html"
        )
    llm = _StepConv(
        [
            'records("li.row")',
            'field(name, wq.doc.select(".name").attr("text"))',
            'detail("a.more")',
            'detail_field(body, wq.doc.select("article.body").attr("text"))',
            "done()",
        ]
    )
    art = _steps_art(httpserver, llm, ["name", "body"])
    assert isinstance(art, QueryArtifact)
    assert art.complete, art.reason  # body is present -- inside the nested detail branch
    after_detail = llm.turns[3]
    assert "followed " in after_detail and "article.body" in after_detail
    assert 'detail.body: "Body One", "Body Two"' in llm.turns[4]
    assert art.sample[0] == {"name": "A", "detail": {"body": "Body One"}}


def test_steps_engine_reverts_a_failed_op_and_rejects_prose(httpserver: HTTPServer) -> None:
    # prose -> NOT APPLIED; a 0-match record selector -> not applied; a field whose selector misses
    # -> the probe fails and the op is REVERTED (the draft stays runnable); the loop then completes.
    from web.onboard import QueryArtifact

    httpserver.expect_request("/list").respond_with_data(_LISTING, content_type="text/html")
    llm = _StepConv(
        [
            "I think the rows are li.row, let me select them.",
            'records(".nope")',
            'records("li.row")',
            'field(name, wq.doc.select(".missing").attr("text"))',
            'field(name, wq.doc.select(".name").attr("text"))',
            "done()",
        ]
    )
    art = _steps_art(httpserver, llm, ["name"])
    assert isinstance(art, QueryArtifact)
    assert art.complete and art.row_count == 2, art.reason
    assert "NOT APPLIED: not an op call" in llm.turns[1]
    assert "matched 0 elements" in llm.turns[2]
    assert "REVERTED" in llm.turns[4] and "select_miss" in llm.turns[4]
    assert "QUERY SO FAR:\nwq.doc.select_all('li.row')" in llm.turns[4]  # the draft kept


def test_parse_op_rejects_bad_calls() -> None:
    from web.onboard.author_steps import StepError, parse_op

    assert parse_op('```\nrecords("li.x")\n```').line() == "records(li.x)"
    assert parse_op("field(title, wq.doc.select('h2').attr('text')) — done").args[0] == "title"
    for bad in ("hello", "records()", "field(1x, wq.doc)", "field(a, 'text')", "nope('x')"):
        try:
            parse_op(bad)
        except StepError:
            continue
        raise AssertionError(f"{bad!r} must be rejected")
