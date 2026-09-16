"""The onboarding pipeline end-to-end, offline: a stub search + a stub LLM over a
local two-page "company" site. Proves each stage wires to the next and that the
authored query actually extracts the dataset when run."""

import json

import httpx
import pytest

from webclient import WebClient, from_blob, wq
from webclient.core.document.models import Flag
from webclient.pipelines import (
    Brief,
    Budget,
    BudgetExceeded,
    Candidate,
    LlmClient,
    SearchHit,
    Usage,
    evaluate_candidate,
    onboard_company,
    price_for,
    render_prompt,
    write_resolve,
)

HOME = """
<html><head><title>Acme</title></head><body>
  <nav><a href="/products">Products</a><a href="/about">About</a></nav>
  <main><h1>Acme</h1><p>We make widgets.</p></main>
  <footer><a href="/privacy">Privacy</a><a href="https://twitter.com/acme">Twitter</a></footer>
</body></html>
"""
PRODUCTS = """
<html><head><title>Products - Acme</title></head><body><main>
  <div class="product"><span class="name">Widget</span><span class="price">$10</span></div>
  <div class="product"><span class="name">Sprocket</span><span class="price">$20</span></div>
  <div class="product"><span class="name">Cog</span><span class="price">$30</span></div>
</main></body></html>
"""


@pytest.fixture
def site(httpserver):
    httpserver.expect_request("/").respond_with_data(HOME, content_type="text/html")
    httpserver.expect_request("/products").respond_with_data(
        PRODUCTS, content_type="text/html"
    )
    httpserver.expect_request("/about").respond_with_data(
        "<html><body><h1>About</h1></body></html>", content_type="text/html"
    )
    return httpserver


def test_onboard_company_finds_and_queries_the_dataset(site):
    products_url = site.url_for("/products")

    # the query the model is expected to author for this page (built here so the stub
    # can hand back its blob -- in production the LLM writes this from the skeleton).
    expected = (
        wq.doc.select_all(".product")  # document-rooted: the caller supplies the doc
        .extract(
            name=wq.doc.select(".name").text_content,
            price=wq.doc.select(".price").text_content,
        )
        .project()
    )
    blob = expected.to_blob()

    def search(query, k):  # a stub SearchFn: seed at the company home page
        assert "widget" in query.lower() or "product" in query.lower()
        return [SearchHit(url=site.url_for("/"), title="Acme", snippet="widgets")]

    def llm(prompt: str) -> str:  # a scripted model, routed by prompt content
        if "web-search query" in prompt:
            return "acme products widgets"
        if "frontier links" in prompt:  # pick the /products link by its listing index
            for line in prompt.splitlines():
                s = line.strip()
                if s[:1].isdigit() and "/products" in s:
                    return f"[{s.split('.', 1)[0]}]"
            return "[]"
        if "crawled pages" in prompt:  # the products page is the must-evaluate source
            return json.dumps(
                [{"url": products_url, "kind": "page", "tier": "must", "note": "list"}]
            )
        if "Assess this page" in prompt:
            return json.dumps(
                {
                    "dataset_present": True,
                    "is_queryable": True,
                    "completeness": "full",
                    "has_pagination": False,
                    "scrapability": 9,
                    "verdict": "a full product list",
                }
            )
        if "query DSL" in prompt or "portable blob" in prompt:
            return f"here is the query:\n```json\n{blob}\n```"
        return "{}"

    with WebClient() as wc:
        result = onboard_company(
            "Acme", Brief(description="the company's products", fields=["name", "price"]),
            wc=wc, llm=llm, search=search, browser=False,
        )

    assert result.ok, result.reason
    assert result.evaluation is not None and result.evaluation.url == products_url
    assert result.evaluation.dataset_present and result.evaluation.is_queryable
    # write_resolve was deterministic from signals: a plain static page needs nothing
    assert result.resolve is not None and result.resolve.browser is None
    assert result.query is not None and ".project()" in result.query.describe
    # the query was tested at authoring time -- it ran against the source and extracted
    assert result.query.tested and result.query.row_count == 3
    assert result.query.sample  # a small produced-row sample is kept

    # the output is loadable as a plan too (not just the blob), and both run the same
    from webclient import from_plan

    with WebClient() as wc:
        doc = wc.fetch(products_url)  # the caller fetches; the query is doc-level
        rows = from_blob(result.query.blob).collect(doc)
        plan_rows = from_plan(result.query.plan, wc).collect(doc)
    assert [r["name"] for r in rows] == ["Widget", "Sprocket", "Cog"]
    assert rows[0]["price"] == "$10"
    assert [r["name"] for r in plan_rows] == ["Widget", "Sprocket", "Cog"]  # plan == blob

    # richer logging: the run left a readable step trace on the result
    assert result.steps and any("query authored" in s for s in result.steps)
    assert any("crawled" in s for s in result.steps)
    assert result.cost_usd == 0.0  # the stub llm carries no cost


def test_onboard_company_reports_when_no_seeds(site):
    def search(query, k):
        return []

    with WebClient() as wc:
        result = onboard_company(
            "Nobody", Brief(description="ghosts"), wc=wc,
            llm=lambda p: "{}", search=search, browser=False,
        )
    assert not result.ok and result.reason == "no search seeds"


def test_write_resolve_maps_flags_to_policy():
    # the flags deterministically choose the transport policy for the source.
    spa = write_resolve([Flag(name="spa", present=True, remedy="browser")])
    assert spa.browser is not None and spa.browser.when == "always" and spa.proxy is None

    stealth = write_resolve([Flag(name="anti_bot_triggered", present=True, remedy="stealth")])
    assert stealth.browser is not None and stealth.proxy is not None
    assert stealth.antibot is not None and stealth.antibot.level == "stealth"

    proxy = write_resolve([Flag(name="anti_bot_triggered", present=True, remedy="proxy")])
    assert proxy.proxy is not None and proxy.browser is None and proxy.antibot is None

    assert write_resolve([]).browser is None  # a plain source needs nothing


def test_evaluate_drops_a_login_walled_candidate(httpserver):
    # a login wall blocks the dataset -> the candidate is dropped BEFORE the model is
    # asked (the flag short-circuits), so no query is ever attempted against it.
    httpserver.expect_request("/members").respond_with_data(
        "<h1>Sign in</h1><form><input type='password'></form>", content_type="text/html",
    )

    def boom(prompt):  # the LLM must not be called for a walled candidate
        raise AssertionError("evaluate should not ask the model about a login wall")

    with WebClient() as wc:
        ev = evaluate_candidate(
            Candidate(url=httpserver.url_for("/members")),
            Brief(description="member directory"),
            wc=wc, llm=boom, browser="never",
        )
    assert not ev.dataset_present and ev.verdict == "login required"
    assert "login_required" in ev.flags


# --------------------------------------------------------------------------- #
# Prompts-as-data: the templates load and render with the right variables.
# --------------------------------------------------------------------------- #


def test_prompt_templates_load_and_render():
    # every prompt file loads and renders; the routing substrings the pipeline (and
    # the scripted stub llm) rely on survive the move to data.
    assert "web-search query" in render_prompt(
        "search_query", company="Acme", description="products", fields_line=""
    )
    assert "frontier links" in render_prompt(
        "pick_edges", description="d", fields_line="", listing="0. http://x"
    )
    assert "crawled pages" in render_prompt(
        "select_candidates", description="d", fields_line="", pages_json="[]"
    )
    ev = render_prompt(
        "evaluate_candidate", description="d", fields_line=" Target fields: a.",
        candidate_url="http://c", flag_map_json="{}", endpoints_json="[]", skeleton="SKEL",
    )
    assert "Assess this page" in ev and "http://c" in ev and "SKEL" in ev
    wq_prompt = render_prompt(
        "write_query", guide="GUIDE-TEXT", description="d", fields_line="",
        pager="", skeleton="SKEL",
    )
    assert "query syntax" in wq_prompt and "portable blob" in wq_prompt
    assert wq_prompt.startswith("GUIDE-TEXT")


def test_render_prompt_requires_every_placeholder():
    # a missing variable fails loudly rather than shipping a half-filled prompt.
    with pytest.raises(KeyError):
        render_prompt("search_query", company="Acme")  # no description / fields_line


# --------------------------------------------------------------------------- #
# LlmClient: parses a mocked Messages API response, accumulates cost, no network.
# --------------------------------------------------------------------------- #


def _mock_messages_transport(captured=None, *, in_tok=1000, out_tok=1000):
    """An httpx.MockTransport standing in for the Anthropic Messages API."""
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": "acme products widgets"},
                    {"type": "text", "text": "!"},  # multiple text blocks concatenate
                ],
                "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
            },
        )

    return httpx.MockTransport(handler)


def test_llm_client_parses_response_and_accumulates_cost():
    captured: list[httpx.Request] = []
    client = LlmClient(
        model="claude-opus-5",
        auth="test-key",
        transport=_mock_messages_transport(captured),
    )

    text = client("write a query")
    assert text == "acme products widgets!"  # both text blocks joined
    assert client.last_usage == Usage(input_tokens=1000, output_tokens=1000)

    per_call = Usage(input_tokens=1000, output_tokens=1000).cost_usd(price_for("claude-opus-5"))
    assert per_call == pytest.approx((1000 * 5.0 + 1000 * 25.0) / 1_000_000)
    assert client.spent_usd == pytest.approx(per_call)

    client("and another")  # running spend accumulates across calls
    assert client.spent_usd == pytest.approx(2 * per_call)
    assert client.budget.calls == 2

    # it really spoke the Messages API shape, offline, with the auth header set.
    req = captured[0]
    assert req.url.path == "/v1/messages" and req.headers["x-api-key"] == "test-key"
    body = json.loads(req.content)
    assert body["model"] == "claude-opus-5" and body["messages"][0]["content"] == "write a query"


def test_budget_exceeded_fires_when_cap_is_crossed():
    per_call = Usage(input_tokens=1000, output_tokens=1000).cost_usd(price_for("claude-opus-5"))
    client = LlmClient(
        auth="test-key",
        transport=_mock_messages_transport(),
        budget=Budget(max_usd=per_call),  # room for exactly one call
    )

    client("first call fits the budget")  # charges the budget up to the cap
    with pytest.raises(BudgetExceeded) as exc:
        client("second call is over the cap")
    assert exc.value.spent_usd == pytest.approx(per_call)
    assert exc.value.limit_usd == pytest.approx(per_call)


def test_onboard_company_reports_a_blown_budget(site):
    # a real LlmClient (mocked transport, no network) as the injected llm, capped so
    # the budget trips mid-pipeline -> the run surfaces it gracefully, not by crashing.
    per_call = Usage(input_tokens=1000, output_tokens=1000).cost_usd(price_for("claude-opus-5"))

    def search(query, k):
        return [SearchHit(url=site.url_for("/"), title="Acme", snippet="widgets")]

    client = LlmClient(
        auth="test-key",
        transport=_mock_messages_transport(),
        budget=Budget(max_usd=per_call),  # the second LLM call will trip
    )
    with WebClient() as wc:
        result = onboard_company(
            "Acme", Brief(description="the company's products"),
            wc=wc, llm=client, search=search, browser=False,
        )
    assert not result.ok
    assert result.reason == "llm budget exceeded"
    assert client.spent_usd == pytest.approx(per_call)  # stopped at the cap


def test_brief_loads_from_markdown_frontmatter():
    from webclient.pipelines.onboarding import Brief

    md = """---
name: product-catalogue
title: Product Catalogue
schema:
  - name: the product's display name
  - price: the price object
  - price.value: the numeric amount
  - price.unit: the currency or unit
look:
  - product and pricing listing pages
ignore:
  - blog, careers and legal pages
crawl:
  max_pages: 30
  depth: 2
  browser: false
---
The company's full product catalogue.
"""
    brief = Brief.from_markdown(md)
    assert brief.name == "product-catalogue" and brief.title == "Product Catalogue"
    # nested schema via dotted paths, each with a description
    assert brief.fields == ["name", "price", "price.value", "price.unit"]
    assert brief.descriptions["price.value"] == "the numeric amount"
    # look/ignore are natural-language guides, not URL fragments
    assert brief.look == ["product and pricing listing pages"]
    # crawl block is a native YAML mapping (int/bool typed by yaml)
    assert brief.crawl == {"max_pages": 30, "depth": 2, "browser": False}
    assert brief.description.startswith("The company's full product catalogue")


def test_filter_frontier_collapses_pagination_and_similar_apis():
    from webclient.core.crawl import Edge
    from webclient.pipelines.onboarding import Brief, _filter_frontier

    # STRUCTURAL de-dup only (look/ignore are NL guides now, applied by the model): a
    # paginated set + repeated similar-API calls collapse; distinct resources survive.
    brief = Brief(description="products")
    edges = [
        Edge(url="https://x.co/list?page=1"),
        Edge(url="https://x.co/list?page=2"),   # same API, different value -> collapsed
        Edge(url="https://x.co/list/page/3"),   # paginated path -> collapsed
        Edge(url="https://x.co/item/1"),        # distinct resource -> kept
        Edge(url="https://x.co/item/2"),        # distinct resource -> kept
    ]
    kept = [e.url for e in _filter_frontier(edges, brief)]
    assert kept == [
        "https://x.co/list?page=1",
        "https://x.co/item/1",
        "https://x.co/item/2",
    ]


def test_brief_schema_tree_carries_descriptions():
    from webclient.pipelines.onboarding import Brief, _fields_line

    brief = Brief(
        fields=["name", "price.value", "price.unit"],
        descriptions={"price": "the price object", "price.value": "numeric amount"},
    )
    assert brief.is_nested
    tree = brief.schema_tree()
    price = next(f for f in tree if f.name == "price")
    assert price.description == "the price object"
    assert {c.name for c in price.children} == {"value", "unit"}
    assert next(c for c in price.children if c.name == "value").description == "numeric amount"
    # the nested schema + descriptions are rendered into every prompt's hint block
    line = _fields_line(brief)
    assert "- price — the price object" in line and "- value — numeric amount" in line


def test_nested_extract_outputs_nested_json():
    from webclient import Document, default_client, wq

    d = Document(
        content=b'<div class="product"><span class="name">A</span>'
        b'<span class="price">30 $ /1TB</span></div>',
        kind="html",
        status_code=200,
    )
    d._client = default_client()
    row = d.select(".product").extract(
        name=wq.doc.select(".name").text_content,
        price=wq.doc.select(".price").extract(
            value=wq.doc.regex(r"[\d.]+"),
            unit=wq.doc.regex(r"[\d.]+\s*(\S+)", group=1),
        ).project(),
    ).project()
    assert row == {"name": "A", "price": {"value": "30", "unit": "$"}}


def test_query_runs_across_multiple_base_urls(httpserver):
    # a dataset split across distinct URLs (not pagination): one authored query runs
    # against every base_url and run_query unions the rows.
    from webclient.pipelines import run_query
    from webclient.pipelines.onboarding import QueryArtifact

    for path, items in (("/cloud", ["Cloud A", "Cloud B"]), ("/onprem", ["OnPrem X"])):
        html = "".join(f'<div class="product"><span class="name">{n}</span></div>' for n in items)
        httpserver.expect_request(path).respond_with_data(
            f"<main>{html}</main>", content_type="text/html"
        )
    query = (
        wq.doc.select_all(".product")  # document-rooted; run_query fetches each base
        .extract(name=wq.doc.select(".name").text_content).project()
    )
    art = QueryArtifact(
        blob=query.to_blob(), describe=query.explain(),
        base_urls=[httpserver.url_for("/cloud"), httpserver.url_for("/onprem")],
    )
    with WebClient() as wc:
        rows = run_query(art, wc=wc)
    assert [r["name"] for r in rows] == ["Cloud A", "Cloud B", "OnPrem X"]  # unioned


def test_model_price_includes_cache_read_and_write_costs():
    from webclient.pipelines import Usage
    from webclient.pipelines.llm import ModelPrice, price_for

    price = price_for("claude-opus-5")  # input 5, output 25 per MTok
    # cache write ~1.25x input, cache read ~0.10x input -- first-class fields now
    assert price.cache_write_usd_per_mtok == pytest.approx(6.25)
    assert price.cache_read_usd_per_mtok == pytest.approx(0.5)

    usage = Usage(
        input_tokens=1000, output_tokens=2000,
        cache_write_tokens=4000, cache_read_tokens=8000,
    )
    expected = (1000 * 5.0 + 2000 * 25.0 + 4000 * 6.25 + 8000 * 0.5) / 1_000_000
    assert usage.cost_usd(price) == pytest.approx(expected)

    # an explicit override (e.g. a 1-hour cache at 2x write) is possible
    hourly = ModelPrice.of(5.0, 25.0, cache_write_mult=2.0)
    assert hourly.cache_write_usd_per_mtok == pytest.approx(10.0)


def test_cli_positional_args_and_brief_by_name(monkeypatch, tmp_path):
    # the CLI is `onboard <brief> <company>...`: a brief path OR a packaged name, then
    # one or more companies. Without an API key it errors cleanly (SystemExit).
    from webclient.pipelines.__main__ import _load_brief, main

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    brief = tmp_path / "b.md"
    brief.write_text("---\nname: t\nschema:\n  - name: the name\n---\nA dataset.\n")

    with pytest.raises(SystemExit):  # brief path + a company, no key -> clean exit
        main([str(brief), "Acme", "Globex"])
    with pytest.raises(SystemExit):  # no company at all is an argparse error
        main([str(brief)])

    # a packaged brief resolves by name (with - / _ interchangeable)
    assert _load_brief("ir-news").name == "ir-news"
    assert _load_brief("product_catalogue").fields  # the shipped example


def test_ask_json_retries_with_the_parser_error():
    # a decode failure re-prompts the model with its bad output + the error, so it can
    # fix it; the second reply is parsed.
    from webclient.pipelines.onboarding import _ask_json

    replies = ["not json at all", '{"ok": true}']
    seen_prompts: list[str] = []

    def llm(prompt: str) -> str:
        seen_prompts.append(prompt)
        return replies[len(seen_prompts) - 1]

    result = _ask_json(llm, "give me json", retries=1)
    assert result == {"ok": True}
    assert len(seen_prompts) == 2  # retried once
    assert "could not be parsed as JSON" in seen_prompts[1]  # the error was supplied


def test_ask_json_gives_up_after_retries():
    from webclient.pipelines.onboarding import _ask_json

    assert _ask_json(lambda p: "still not json", "give me json", retries=1) is None


def test_model_pricing_is_configurable_on_the_client():
    from webclient.pipelines.llm import LlmClient, ModelPrice, PRICING

    client = LlmClient(model="claude-opus-5", auth="k",
                       pricing={**PRICING, "claude-opus-5": ModelPrice.of(6.0, 30.0)})
    assert client.price().input_usd_per_mtok == 6.0  # the override wins
    # a plain client uses the default table
    assert LlmClient(model="claude-opus-5", auth="k").price().input_usd_per_mtok == 5.0


def test_settings_pass_pricing_overrides_to_the_llm_client():
    from webclient import LlmSettings, Settings
    from webclient.pipelines.llm import ModelPrice

    s = Settings(llm=LlmSettings(model="claude-opus-5",
                                 pricing={"claude-opus-5": ModelPrice.of(7.0, 35.0)}))
    llm = s.llm_client(auth="k")
    assert llm.price().input_usd_per_mtok == 7.0
    llm.close()


def test_summary_prints_scores_flags_reference_resolve_and_a_table():
    import io
    import logging as _logging

    from webclient.core.reference.models import BrowserPolicy, ProxyPolicy, Resolve
    from webclient.pipelines import onboarding as ob
    from webclient.pipelines.onboarding import CandidateEval, OnboardingResult, QueryArtifact

    r = OnboardingResult(
        company="Acme", brief=Brief(), ok=True, cost_usd=0.0231,
        evaluation=CandidateEval(
            url="https://acme/products", dataset_present=True, is_queryable=True,
            completeness="full", has_pagination=True, scrapability=8,
            flags={"spa": 0.9, "pagination": 0.62},
        ),
        resolve=Resolve(browser=BrowserPolicy(when="always", stealth=True), proxy=ProxyPolicy.auto()),
        query=QueryArtifact(
            blob="{}", describe="Document.select_all('.product').extract(...).project()",
            tested=True, row_count=3, base_urls=["https://acme/cloud", "https://acme/onprem"],
            sample=[{"name": "Widget", "price": "$10"}, {"name": "Cog", "price": "$30"}],
        ),
    )
    buf = io.StringIO()
    handler = _logging.StreamHandler(buf)
    ob.log.addHandler(handler)
    ob.log.setLevel(_logging.INFO)
    try:
        ob._summarize(r)
    finally:
        ob.log.removeHandler(handler)
    text = buf.getvalue()

    assert "result:    ready" in text
    assert "scrapability 8/10" in text and "queryable=True" in text  # the scores
    assert "spa 0.90" in text and "pagination 0.62" in text  # all the page's flags
    # the reference (multi-URL) + resolve args, enough to reproduce the fetch
    assert "reference: https://acme/cloud, https://acme/onprem" in text
    assert "browser=always, stealth, proxy=on" in text
    # the tested output rendered as a table (columns from the row keys)
    assert "name" in text and "price" in text and "Widget" in text and "$10" in text
    assert "spent:     $0.0231" in text


def test_api_docs_page_is_never_a_data_source(httpserver):
    # the "docs page mistaken for the API" fix: even if the model marks a page queryable,
    # is_api_docs=True forces dataset_present/is_queryable false so it is not chosen.
    httpserver.expect_request("/docs/api").respond_with_data(
        "<html><body><h1>API Reference</h1><p>GET /v1/products returns...</p></body></html>",
        content_type="text/html",
    )

    def llm(prompt: str) -> str:
        # the model (wrongly) says queryable, but flags it as API docs
        return json.dumps({
            "dataset_present": True, "is_queryable": True, "is_api_docs": True,
            "scrapability": 8, "verdict": "this is API documentation, not the data",
        })

    with WebClient() as wc:
        ev = evaluate_candidate(
            Candidate(url=httpserver.url_for("/docs/api")),
            Brief(description="products"), wc=wc, llm=llm, browser="never",
        )
    assert ev.is_api_docs
    assert not ev.dataset_present and not ev.is_queryable  # guarded out
    assert ev.verdict  # the reason is captured (and logged)


def test_pick_edges_accepts_reasons_and_bare_indices():
    from webclient.core.crawl import Edge
    from webclient.pipelines.onboarding import _pick_edges

    frontier = [Edge(url="https://x/a"), Edge(url="https://x/b"), Edge(url="https://x/c")]
    # reasoned objects
    picks = _pick_edges(lambda p: '[{"n": 1, "why": "the data endpoint"}]',
                        Brief(description="d"), frontier)
    assert picks == ["https://x/b"]
    # bare indices still work (robustness)
    picks = _pick_edges(lambda p: "[0, 2]", Brief(description="d"), frontier)
    assert picks == ["https://x/a", "https://x/c"]


def _ok_response() -> "httpx.Response":
    return httpx.Response(200, json={
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })


def test_llm_retries_a_500_then_succeeds():
    from webclient.pipelines.llm import LlmClient

    calls = {"n": 0}

    def handler(req: "httpx.Request") -> "httpx.Response":
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, json={"error": {"type": "overloaded", "message": "retry"}})
        return _ok_response()

    client = LlmClient(model="claude-opus-5", auth="k", retry_backoff=0.0,
                       transport=httpx.MockTransport(handler))
    assert client("hi") == "ok" and calls["n"] == 2  # retried once, then 200


def test_llm_surfaces_a_400_with_the_api_message():
    from webclient.pipelines.llm import LlmClient, LlmError

    def handler(req: "httpx.Request") -> "httpx.Response":
        return httpx.Response(400, json={
            "error": {"type": "invalid_request_error", "message": "prompt is too long: 300000 tokens"}
        })

    client = LlmClient(model="claude-opus-5", auth="k",
                       transport=httpx.MockTransport(handler))
    with pytest.raises(LlmError) as exc:
        client("hi")
    assert exc.value.status_code == 400 and "too long" in exc.value.message


def test_llm_gives_up_after_max_retries():
    from webclient.pipelines.llm import LlmClient, LlmError

    def handler(req: "httpx.Request") -> "httpx.Response":
        return httpx.Response(529, json={"error": {"type": "overloaded", "message": "busy"}})

    client = LlmClient(model="claude-opus-5", auth="k", max_retries=2, retry_backoff=0.0,
                       transport=httpx.MockTransport(handler))
    with pytest.raises(LlmError):
        client("hi")


def test_min_interval_rate_limits(monkeypatch):
    from webclient.pipelines import llm as llm_mod

    slept: list[float] = []
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: slept.append(s))
    client = llm_mod.LlmClient(model="claude-opus-5", auth="k", min_interval=0.5,
                               transport=httpx.MockTransport(lambda r: _ok_response()))
    client("a")
    client("b")  # the second call must wait out the interval
    assert any(0.0 < s <= 0.5 for s in slept)


def test_ask_json_survives_an_llm_error():
    from webclient.pipelines.llm import LlmError
    from webclient.pipelines.onboarding import _ask_json

    def boom(prompt: str) -> str:
        raise LlmError(400, "prompt is too long")

    assert _ask_json(boom, "give me json") is None  # doesn't crash the pipeline


def test_clip_bounds_and_notes_truncation():
    from webclient.pipelines.onboarding import _clip

    assert _clip("short", 100, "x") == "short"  # under budget: unchanged
    # head (default): keeps the start
    out = _clip("y" * 500, 100, "skeleton")
    assert out.startswith("y" * 100) and "trimmed" in out and "skeleton" in out
    # html: keeps the CENTRE (chrome at the ends is dropped)
    html = _clip("A" * 50 + "M" * 100 + "Z" * 50, 100, "skeleton", kind="html")
    assert "M" * 100 in html and "trimmed" in html
    # json: keeps both ENDS (the repetitive middle is dropped)
    js = _clip("HEAD" + "x" * 500 + "TAIL", 100, "pages", kind="json")
    assert js.startswith("HEAD") and js.endswith("TAIL") and "trimmed" in js


def test_evaluate_clips_a_huge_page_skeleton(httpserver):
    # a giant page must not blow the prompt: the skeleton is clipped to the char budget.
    from webclient.pipelines.onboarding import _MAX_SKELETON_CHARS

    # distinct per-row structure so the skeleton's identical-sibling merge can't shrink
    # it -- forcing it past the char budget (a repetitive real listing stays tiny).
    rows = "".join(
        f'<section class="prod-{i}" data-x="{i}"><h3 class="n-{i}">P{i}</h3>'
        f'<span class="p-{i}">v{i}</span></section>' for i in range(3000)
    )
    httpserver.expect_request("/big").respond_with_data(
        f"<main>{rows}</main>", content_type="text/html"
    )
    captured: dict[str, str] = {}

    def llm(prompt: str) -> str:
        if "Assess this page" in prompt:
            captured["eval"] = prompt
            return '{"dataset_present": true, "scrapability": 5, "verdict": "ok"}'
        return "{}"

    with WebClient() as wc:
        evaluate_candidate(
            Candidate(url=httpserver.url_for("/big")),
            Brief(description="products"), wc=wc, llm=llm, browser="never",
        )
    assert "trimmed" in captured["eval"]  # the big skeleton was clipped (centre kept)
    # the prompt is bounded (skeleton budget + the fixed prompt scaffolding)
    assert len(captured["eval"]) < _MAX_SKELETON_CHARS + 4000
