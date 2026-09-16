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
        wq.ref.resolve()
        .select_all(".product")
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
        rows = from_blob(result.query.blob).collect(wc.ref(products_url))
        plan_rows = from_plan(result.query.plan, wc).collect(wc.ref(products_url))
    assert [r["name"] for r in rows] == ["Widget", "Sprocket", "Cog"]
    assert rows[0]["price"] == "$10"
    assert [r["name"] for r in plan_rows] == ["Widget", "Sprocket", "Cog"]  # plan == blob


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
    assert "query DSL" in wq_prompt and "portable blob" in wq_prompt
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
  - max_pages: 30
  - depth: 2
  - browser: false
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
    # crawl block configures the pipeline (coerced to int/bool)
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
        wq.ref.resolve().select_all(".product")
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
