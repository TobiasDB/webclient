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

    # the query the model is expected to author for this page. In production the model
    # WRITES this query code; the pipeline evals it (loads it as written) rather than
    # asking the model to hand-serialize a blob.
    code = (
        'wq.doc.select_all(".product").extract('
        'name=wq.doc.select(".name").text_content, '
        'price=wq.doc.select(".price").text_content).project()'
    )

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
        if "query code" in prompt or "write a query" in prompt:
            return f"here is the query:\n{code}"
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


def test_select_candidates_forces_data_docs_and_fails_open():
    from webclient.core.document.models import PageCard
    from webclient.pipelines.onboarding import select_candidates

    brief = Brief(description="news", fields=["title", "date"])

    class _Crawl:  # a stand-in for the finished Crawl (only .pages is read)
        pass

    def empty_llm(_prompt: str) -> str:
        return "[]"  # the candidate filter returns NOTHING (the cheapest-model variance case)

    # a SEEDED feed is a data document -> forced in as a MUST candidate even though the LLM
    # filter picked nothing; a NON-seed feed the crawl merely discovered is NOT forced in
    # (a site can expose many feeds -- only what we were pointed at counts).
    crawl = _Crawl()
    crawl.pages = [
        PageCard(url="https://acme.com/news", kind="html", title="Newsroom"),
        PageCard(url="https://acme.com/feed.rss", kind="xml", title="RSS"),          # the seed
        PageCard(url="https://acme.com/comments/feed", kind="xml", title="Comments"),  # a stray feed
    ]
    cands = select_candidates(crawl, brief, llm=empty_llm,
                              seed_urls=["https://acme.com/feed.rss"])
    feed = next((c for c in cands if c.url.endswith("feed.rss")), None)
    assert feed is not None and feed.tier == "must"
    assert not any(c.url.endswith("comments/feed") for c in cands)  # stray feed NOT forced in

    # no data docs + an empty filter -> FAIL OPEN: keep the crawled page(s) for evaluation
    crawl2 = _Crawl()
    crawl2.pages = [PageCard(url="https://acme.com/press", kind="html", title="Press")]
    cands2 = select_candidates(crawl2, brief, llm=empty_llm)
    assert [c.url for c in cands2] == ["https://acme.com/press"]
    assert cands2[0].tier == "could"


def test_skeleton_surfaces_an_injected_json_island():
    # records inlined in a <script type=application/json> island are invisible in the DOM
    # skeleton (scripts are stripped) -- they must be surfaced so the model uses .as_json().
    from webclient import Document

    html = (b'<html><body><div id="grid"></div>'
            b'<script id="__DATA__" type="application/json">'
            b'{"catalog": {"items": [{"sku": "A-1"}, {"sku": "B-2"}]}}'
            b"</script></body></html>")
    skel = Document(url="https://x/", kind="html", content=html, status_code=200).skeleton()
    assert "injected JSON island" in skel
    assert "script#__DATA__" in skel and "as_json()" in skel
    assert "catalog" in skel  # a shape preview so the model can write dotted paths


def test_timeliness_gate_uses_the_inter_row_interval():
    import datetime

    from webclient.pipelines.onboarding import _timeliness

    news = Brief(description="ir news", fields=["title", "date"])
    today = datetime.date.today()

    def d(days_ago: int) -> str:
        return (today - datetime.timedelta(days=days_ago)).strftime("%B %d, %Y")

    # cadence ~10 days, newest 2 days ago -> the gap to now is within cadence -> TIMELY
    fresh = [{"date": d(2)}, {"date": d(12)}, {"date": d(22)}, {"date": d(32)}]
    note, stale = _timeliness(fresh, news)
    assert not stale and "within cadence" in note

    # same ~10-day cadence but the newest is 200 days ago -> the gap dwarfs the cadence,
    # so the most recent items are MISSING -> STALE (a self-calibrating bar, not a fixed one)
    old = [{"date": d(200)}, {"date": d(210)}, {"date": d(220)}, {"date": d(230)}]
    note, stale = _timeliness(old, news)
    assert stale and "MISSING" in note

    # no date field -> nothing to judge
    assert _timeliness([{"name": "a"}], Brief(description="p", fields=["name"])) == ("", False)


def test_timeliness_reads_a_nested_date_field_and_ignores_lookalike_names():
    import datetime

    from webclient.pipelines.onboarding import _date_field_paths, _timeliness

    # the date lives under a nested branch (meta.published); "timezone"/"runtime" are NOT dates
    brief = Brief(description="posts", fields=["title", "meta.published", "timezone", "runtime"])
    assert _date_field_paths(brief) == ["meta.published"]
    today = datetime.date.today()

    def d(n: int) -> str:
        return (today - datetime.timedelta(days=n)).strftime("%Y-%m-%d")

    stale = [{"meta": {"published": d(300)}}, {"meta": {"published": d(310)}},
             {"meta": {"published": d(320)}}]
    _, is_stale = _timeliness(stale, brief)
    assert is_stale  # nested date is read + far beyond cadence -> stale


def test_seeds_for_company_drops_look_alike_companies():
    from webclient.pipelines.onboarding import Seed, _seeds_for_company

    seeds = [
        Seed(url="https://www.squarepoint-capital.com/", title="Squarepoint Capital"),
        Seed(url="https://squareup.com/", title="Square — run your business"),
        Seed(url="https://en.wikipedia.org/wiki/Squarepoint", title="Squarepoint Capital – Wikipedia"),
    ]

    def llm(prompt):
        assert "Squarepoint" in prompt  # the exact company is named
        return '{"belong": [0, 2], "note": "excluded Square (squareup.com)"}'

    kept = _seeds_for_company(seeds, "Squarepoint", Brief(description="ir news"), llm)
    assert [s.url for s in kept] == [
        "https://www.squarepoint-capital.com/", "https://en.wikipedia.org/wiki/Squarepoint",
    ]


def test_seeds_for_company_fails_open_without_a_usable_judgement():
    from webclient.pipelines.onboarding import Seed, _seeds_for_company

    seeds = [Seed(url="https://a/"), Seed(url="https://b/")]
    # a model that returns nothing usable, or no model at all -> keep every seed (never
    # silently drop them all on a bad reply)
    assert len(_seeds_for_company(seeds, "X", Brief(description="d"), lambda p: "{}")) == 2
    assert len(_seeds_for_company(seeds, "X", Brief(description="d"), None)) == 2


def test_search_web_retries_stricter_when_all_seeds_are_the_wrong_company():
    from webclient.pipelines.onboarding import search_web

    calls = {"n": 0}

    def search(query, k):
        calls["n"] += 1
        if calls["n"] == 1:  # the first query pulls the look-alike company
            return [SearchHit(url="https://squareup.com/", title="Square")]
        return [SearchHit(url="https://squarepoint.com/", title="Squarepoint Capital")]

    def llm(prompt):
        if "web-search query" in prompt:  # craft (and, on retry, disambiguate) the query
            return "squarepoint capital official" if "DIFFERENT company" in prompt else "squarepoint"
        if "belong" in prompt:  # verify: the right one belongs only in the second set
            return '{"belong": [0]}' if "https://squarepoint.com" in prompt else '{"belong": []}'
        return "{}"

    seeds = search_web(Brief(description="ir news"), "Squarepoint", search=search, llm=llm)
    assert calls["n"] == 2  # it retried the search with a stricter query
    assert [s.url for s in seeds] == ["https://squarepoint.com/"]


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
        "pick_edges", company="Acme", description="d", fields_line="", listing="0. http://x"
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
    assert "query syntax" in wq_prompt and "query code" in wq_prompt
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


def _pipeline_stub(products_url, *, reviews):
    """A scripted model for the whole pipeline over the ``site`` fixture; ``reviews`` maps a
    review-stage marker (e.g. "CRAWL stage") to the JSON reply to give for it."""
    code = ('wq.doc.select_all(".product").extract('
            'name=wq.doc.select(".name").text_content, '
            'price=wq.doc.select(".price").text_content).project()')

    def llm(prompt: str) -> str:
        # review markers first -- they are specific ("SELECT stage" etc.) and a review
        # prompt can also mention pipeline phrases (the select review lists "crawled pages")
        for marker, reply in reviews.items():
            if marker in prompt:
                return reply
        if "web-search query" in prompt:
            return "acme products"
        if "frontier links" in prompt:
            for line in prompt.splitlines():
                s = line.strip()
                if s[:1].isdigit() and "/products" in s:
                    return f'[{s.split(".", 1)[0]}]'
            return "[]"
        if "crawled pages" in prompt:
            return json.dumps([{"url": products_url, "kind": "page", "tier": "must", "note": "list"}])
        if "Assess this page" in prompt:
            return json.dumps({"dataset_present": True, "is_queryable": True, "completeness": "full",
                               "has_pagination": False, "scrapability": 9, "verdict": "a full product list"})
        if "query code" in prompt or "write a query" in prompt:
            return code
        return "{}"

    return llm


def _acme_search(site):
    def search(q, k):
        return [SearchHit(url=site.url_for("/"), title="Acme")]
    return search


def test_a_failing_stage_review_is_a_flag_not_a_gate(site):
    # reviews are INFORMATION for the human, not hard gates: a failing crawl review is recorded
    # but the run CONTINUES and still ships a working query (ship is driven by the deterministic
    # extraction, not a model's opinion).
    products_url = site.url_for("/products")
    llm = _pipeline_stub(products_url, reviews={
        "CRAWL stage": '{"pass": false, "verdict":"poor","score":2,"issues":["missed the catalogue"],"summary":"crawl looked thin"}',
    })
    with WebClient() as wc:
        result = onboard_company(
            "Acme", Brief(description="the company's products", fields=["name", "price"]),
            wc=wc, llm=llm, search=_acme_search(site), browser=False, review=True,
        )
    by = {r.stage: r for r in result.reviews}
    assert "crawl" in by and not by["crawl"].passed      # recorded as a FLAG
    assert result.ok                                     # ...but the run was NOT abandoned
    assert result.query is not None and result.query.complete
    assert not result.reason                             # not failed by the review


def test_review_passes_let_the_pipeline_complete(site):
    # when every stage review passes, the pipeline completes and the reviews are recorded.
    products_url = site.url_for("/products")
    ok = '{"pass": true, "verdict":"good","score":9,"issues":[],"summary":"looks right"}'
    llm = _pipeline_stub(products_url, reviews={
        "CRAWL stage": ok, "SELECT stage": ok, "QUERY stage": ok,
    })
    with WebClient() as wc:
        result = onboard_company(
            "Acme", Brief(description="the company's products", fields=["name", "price"]),
            wc=wc, llm=llm, search=_acme_search(site), browser=False, review=True,
        )
    assert result.ok, result.reason
    by = {r.stage: r for r in result.reviews}
    assert {"crawl", "select", "query"} <= set(by) and all(by[s].passed for s in ("crawl", "select", "query"))
    assert "failure" not in by  # success -> no diagnosis
    assert result.query is not None and result.query.row_count == 3


def test_query_review_is_recorded_but_does_not_fail_a_working_query(site):
    # a query can RUN and extract valid data yet be graded low by the model; the query review is
    # RECORDED as a flag for the human, but it does NOT discard a working query -- ship is
    # decided by the deterministic extraction (complete rows), not the model's opinion.
    products_url = site.url_for("/products")
    ok = '{"pass": true, "verdict":"good","score":9,"issues":[],"summary":"ok"}'
    llm = _pipeline_stub(products_url, reviews={
        "CRAWL stage": ok, "SELECT stage": ok,
        "QUERY stage": '{"pass": false, "verdict":"poor","score":3,"issues":["price is the wrong field"],"summary":"output does not match the brief"}',
    })
    with WebClient() as wc:
        result = onboard_company(
            "Acme", Brief(description="the company's products", fields=["name", "price"]),
            wc=wc, llm=llm, search=_acme_search(site), browser=False, review=True,
        )
    assert result.query is not None and result.query.row_count == 3  # the query DID run
    assert result.ok and not result.reason              # ...and ships despite the low review
    by = {r.stage: r for r in result.reviews}
    assert "query" in by and not by["query"].passed     # recorded as a FLAG


def test_optional_schema_fields_are_marked_and_rendered():
    from webclient.pipelines.onboarding import Brief, _fields_line

    # a trailing `?` marks a field optional, both directly and from markdown frontmatter
    brief = Brief(fields=["name", "sku?", "price.value", "price.discount?"])
    assert brief.fields == ["name", "sku", "price.value", "price.discount"]  # `?` stripped
    assert set(brief.optional) == {"sku", "price.discount"}
    tree = brief.schema_tree()
    assert next(f for f in tree if f.name == "sku").optional
    assert not next(f for f in tree if f.name == "name").optional
    discount = next(c for f in tree if f.name == "price" for c in f.children if c.name == "discount")
    assert discount.optional
    # the outline flags optional fields so the author knows they may be absent
    line = _fields_line(brief)
    assert "- sku (optional)" in line and "- name\n" in line + "\n"  # name has no marker

    md = "---\nschema:\n  - name: the name\n  - sku?: often missing\n---\nx"
    loaded = Brief.from_markdown(md)
    assert loaded.fields == ["name", "sku"] and loaded.optional == ["sku"]


def test_write_query_keeps_records_missing_an_optional_field(httpserver):
    # a query the model writes with .select(..., optional=True) for an optional field keeps
    # records where that field is absent (null), instead of dropping them.
    from webclient.pipelines.onboarding import write_query

    httpserver.expect_request("/p").respond_with_data(
        '<main>'
        '<div class="r"><span class="n">A</span><span class="s">S1</span></div>'
        '<div class="r"><span class="n">B</span></div>'  # no .s here
        '</main>',
        content_type="text/html",
    )
    code = ('wq.doc.select_all(".r").extract('
            'name=wq.doc.select(".n").attr("text"), '
            'sku=wq.doc.select(".s", optional=True).attr("text")).project()')

    with WebClient() as wc:
        art = write_query(httpserver.url_for("/p"),
                          Brief(description="rows", fields=["name", "sku?"]),
                          wc=wc, llm=lambda p: code, browser="never", retries=0)
    assert art is not None and art.row_count == 2  # BOTH records kept
    names = [r["name"] for r in art.sample]
    assert names == ["A", "B"] and art.sample[1].get("sku") in (None, "")  # missing -> null


def test_relative_xpath_field_selector_is_scoped_to_the_record():
    # F10 regression: a `//` XPath field selector inside a record must match WITHIN that
    # record, not leak to the whole document (lxml: element.xpath("//...") is document-wide).
    from webclient import Document, default_client, wq

    html = (b'<div class="r"><span class="n">A</span><time>2025-01-01</time></div>'
            b'<div class="r"><span class="n">B</span><time>2025-02-02</time></div>')
    d = Document(content=html, kind="html", status_code=200)
    d._client = default_client()
    rows = list(wq.doc.select_all(".r").extract(
        n=wq.doc.select(".n").attr("text"),
        t=wq.doc.select("//time").attr("text"),  # relative // -> scoped to each record
    ).project().collect(d))
    assert rows == [{"n": "A", "t": "2025-01-01"}, {"n": "B", "t": "2025-02-02"}]


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
    from webclient.pipelines.onboarding import _executable_query

    # the LLM writes the document-level extraction; the pipeline deterministically wraps
    # it into a self-contained reference+resolve query, run against each base by re-root.
    doc_query = (
        wq.doc.select_all(".product")
        .extract(name=wq.doc.select(".name").text_content).project()
    )
    exe = _executable_query(doc_query, httpserver.url_for("/cloud"), None)
    art = QueryArtifact(
        blob=exe.to_blob(), describe=exe.explain(),
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
            flag_signals={"spa": ["framework_marker (static, 0.60): a react marker"]},
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
    assert "spa (0.90)" in text and "pagination (0.62)" in text  # all the page's flags
    assert "framework_marker (static, 0.60): a react marker" in text  # the flag's SIGNALS
    # the reference (multi-URL) + resolve args, enough to reproduce the fetch
    assert "reference: https://acme/cloud, https://acme/onprem" in text
    assert "browser=always, stealth, proxy=on" in text
    # the tested output rendered as a table (columns from the row keys)
    assert "name" in text and "price" in text and "Widget" in text and "$10" in text
    # the blob is printed on its own line for copying
    assert "query blob (copy" in text
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


def test_data_rows_ignores_selected_elements(httpserver):
    # a query that only selects (no .project()) yields elements, not data -> 0 rows;
    # a projected query yields the extracted dicts.
    from webclient import WebClient, from_blob, wq
    from webclient.pipelines.onboarding import _data_rows

    httpserver.expect_request("/p").respond_with_data(
        "<main>" + "".join(f'<div class="r"><span class="n">P{i}</span></div>' for i in range(4)) + "</main>",
        content_type="text/html",
    )
    with WebClient() as wc:
        doc = wc.fetch(httpserver.url_for("/p"))
        selected = from_blob(wq.doc.select_all(".r").to_blob()).collect(doc)
        assert _data_rows(selected) == []  # elements, not data
        projected = from_blob(
            wq.doc.select_all(".r").extract(n=wq.doc.select(".n").attr("text")).project().to_blob()
        ).collect(doc)
        rows = _data_rows(projected)
        assert rows == [{"n": "P0"}, {"n": "P1"}, {"n": "P2"}, {"n": "P3"}]  # real data


def test_write_query_retries_an_unprojected_query_with_feedback(httpserver):
    # first reply selects without projecting (0 data rows) -> retried with feedback ->
    # the second reply projects, so the sample is DATA (dicts), never element objects.
    from webclient import wq
    from webclient.pipelines.onboarding import write_query

    httpserver.expect_request("/p").respond_with_data(
        "<main>" + "".join(f'<div class="r"><span class="n">P{i}</span></div>' for i in range(3)) + "</main>",
        content_type="text/html",
    )
    unprojected = 'wq.doc.select_all(".r")'  # written code, no project -> 0 data rows
    projected = 'wq.doc.select_all(".r").extract(n=wq.doc.select(".n").attr("text")).project()'
    replies = iter([unprojected, projected])
    prompts: list[str] = []

    def llm(prompt: str) -> str:
        prompts.append(prompt)
        return next(replies)

    with WebClient() as wc:
        art = write_query(httpserver.url_for("/p"), Brief(description="rows"),
                          wc=wc, llm=llm, browser="never", retries=1)
    assert art is not None and art.tested and art.row_count == 3
    assert all(isinstance(r, dict) for r in art.sample)  # data, not element objects
    # the retry got a concrete, human-readable hint: the record selector matched but no
    # fields came out (it never projected)
    assert len(prompts) == 2
    assert 'matched 3 record(s)' in prompts[1] and ".project()" in prompts[1]


def test_executable_query_bakes_the_full_resolve_policy(httpserver):
    # F2: a proxy/antibot source bakes the FULL Resolve into the blob (not just the browser
    # tier), so the shipped query re-fetches WITH the policy instead of un-proxied.
    from webclient import from_blob, wq
    from webclient.core.reference.models import AntiBotPolicy, BrowserPolicy, ProxyPolicy, Resolve
    from webclient.pipelines.onboarding import _executable_query

    r = Resolve(proxy=ProxyPolicy.auto(), antibot=AntiBotPolicy(level="stealth"),
                browser=BrowserPolicy(when="always"))
    doc_q = wq.doc.select_all(".r").extract(n=wq.doc.select(".n").attr("text")).project()
    exe = _executable_query(doc_q, "https://x/p", r)
    blob = exe.to_blob()
    assert "proxy" in blob and "antibot" in blob and "stealth" in blob  # full policy encoded
    # survives a round-trip, and the resolve step still carries the policy
    rt = from_blob(blob)
    step = next(s for s in rt._plan.steps if s.kind == "call" and s.kwargs.get("policy"))
    assert step.kwargs["policy"].value.get("antibot", {}).get("level") == "stealth"

    # a plain source keeps the lean form (just the browser tier, no policy blob)
    plain = _executable_query(doc_q, "https://x/p", Resolve(browser=BrowserPolicy(when="always")))
    assert "policy=" not in plain.explain() and ".resolve(" in plain.explain()


def test_output_query_is_self_contained_and_executable(httpserver):
    # the join of the LLM's extraction with the reference + resolve is deterministic and
    # produces a SELF-CONTAINED blob: from_blob(blob).collect() (no context) fetches,
    # resolves and extracts -- executable as is.
    from webclient import WebClient, from_blob, wq
    from webclient.core.reference.models import Resolve
    from webclient.pipelines.onboarding import _executable_query

    httpserver.expect_request("/p").respond_with_data(
        "<main>" + "".join(f'<div class="r"><span class="n">P{i}</span></div>' for i in range(3)) + "</main>",
        content_type="text/html",
    )
    url = httpserver.url_for("/p")
    # the model supplies ONLY the document-level extraction
    doc_q = wq.doc.select_all(".r").extract(n=wq.doc.select(".n").attr("text")).project()
    exe = _executable_query(doc_q, url, Resolve())
    assert exe.explain().startswith(f"reference('{url}').resolve()")  # reference+resolve baked in
    with WebClient() as wc:
        rows = from_blob(exe.to_blob(), wc).collect()  # no context -- self-contained
    assert rows == [{"n": "P0"}, {"n": "P1"}, {"n": "P2"}]


def test_executable_query_strips_stray_navigation_from_the_model():
    # deterministic join: even if the model prefixed a resolve, only its extraction is
    # used (one reference + one resolve, supplied by the pipeline -- not the model).
    from webclient import wq
    from webclient.core.reference.models import Resolve
    from webclient.pipelines.onboarding import _executable_query

    stray = wq.ref.resolve().select_all(".r").extract(n=wq.doc.select(".n").attr("text")).project()
    exe = _executable_query(stray, "https://x/p", Resolve())
    # exactly one resolve, then the extraction (no double resolve)
    assert exe.explain().count(".resolve(") == 1
    assert exe.explain().startswith("reference('https://x/p').resolve().select_all")


def test_docs_pages_are_hard_banned_from_the_crawl():
    from webclient.core.crawl import Edge
    from webclient.pipelines.onboarding import _filter_frontier, _is_docs_url

    # docs URLs are banned; data endpoints and listings are kept
    assert _is_docs_url("https://x.co/docs/api") and _is_docs_url("https://docs.x.co/y")
    assert _is_docs_url("https://developer.x.co/") and _is_docs_url("https://x.co/api-docs/v1")
    assert not _is_docs_url("https://x.co/products")
    assert not _is_docs_url("https://x.co/api/v1/products.json")  # a DATA endpoint stays

    edges = [
        Edge(url="https://x.co/products"),
        Edge(url="https://x.co/docs/api"),        # banned
        Edge(url="https://x.co/api/v1/items.json"),
        Edge(url="https://developer.x.co/guide"),  # banned
    ]
    kept = [e.url for e in _filter_frontier(edges, Brief(description="items"))]
    assert kept == ["https://x.co/products", "https://x.co/api/v1/items.json"]


def test_write_query_rejects_a_query_with_no_selection(httpserver):
    # a query with no select_all/select can't extract -- it must not be accepted (this
    # was producing `reference(url).resolve()` with nothing after, and 0 rows as success).
    from webclient import wq
    from webclient.pipelines.onboarding import write_query

    httpserver.expect_request("/p").respond_with_data(
        '<main><div class="r"><span class="n">A</span></div></main>', content_type="text/html",
    )
    # first: a degenerate query (just the doc root); then a proper one -- written as code
    replies = iter(["wq.doc",
                    'wq.doc.select_all(".r").extract(n=wq.doc.select(".n").attr("text")).project()'])
    prompts: list[str] = []

    def llm(prompt: str) -> str:
        prompts.append(prompt)
        return next(replies)

    with WebClient() as wc:
        art = write_query(httpserver.url_for("/p"), Brief(description="rows"),
                          wc=wc, llm=llm, browser="never", retries=1)
    assert art is not None and art.row_count == 1  # accepted the projecting query
    assert ".select_all(" in art.describe  # the executable has the selection
    assert "NO selection" in prompts[1]  # the model was told to add one


def test_write_query_hint_names_a_wrong_record_selector(httpserver):
    # when the record selector matches NOTHING, the retry feedback says so in plain words
    # (wrong record selector), not a generic "0 rows" -- so the model can fix the selector.
    from webclient.pipelines.onboarding import write_query

    httpserver.expect_request("/p").respond_with_data(
        '<main><div class="r"><span class="n">A</span></div></main>', content_type="text/html",
    )
    # first selects a class that does not exist (0 matches); then the right one
    replies = iter([
        'wq.doc.select_all(".nope").extract(n=wq.doc.select(".n").attr("text")).project()',
        'wq.doc.select_all(".r").extract(n=wq.doc.select(".n").attr("text")).project()',
    ])
    prompts: list[str] = []

    def llm(prompt: str) -> str:
        prompts.append(prompt)
        return next(replies)

    with WebClient() as wc:
        art = write_query(httpserver.url_for("/p"), Brief(description="rows"),
                          wc=wc, llm=llm, browser="never", retries=1)
    assert art is not None and art.row_count == 1  # recovered on the retry
    assert 'matched NO elements' in prompts[1] and '".nope"' in prompts[1]  # named the culprit


def test_frontier_sticks_to_the_company_domains():
    from webclient.core.crawl import Edge
    from webclient.pipelines.onboarding import Seed, _filter_frontier, _seed_domains

    seeds = [Seed(url="https://www.adobe.com/investor-relations.html"),
             Seed(url="https://news.adobe.com/")]
    domains = _seed_domains(seeds)
    assert domains == {"adobe.com"}  # www / news / milo all collapse to adobe.com

    edges = [
        Edge(url="https://www.adobe.com/investor-relations/investor-news.html"),
        Edge(url="https://milo.adobe.com/tools/caas"),          # same company (adobe.com)
        Edge(url="https://www.microsoft.com/investor-relations"),  # a DIFFERENT company
        Edge(url="https://competitor.example/news"),              # unrelated
    ]
    kept = [e.url for e in _filter_frontier(edges, Brief(description="ir news"), allow_domains=domains)]
    assert kept == [
        "https://www.adobe.com/investor-relations/investor-news.html",
        "https://milo.adobe.com/tools/caas",
    ]  # only the company's own domains survive


def test_crawl_evaluates_seeds_before_fetching_them(httpserver):
    # the seeds are NOT all blindly fetched: round 0 lets the model evaluate the seeds and
    # pick which to fetch, so a rejected seed is never crawled.
    from webclient.pipelines.onboarding import Seed, crawl_from_seeds

    httpserver.expect_request("/keep").respond_with_data("<p>data</p>", content_type="text/html")
    httpserver.expect_request("/skip").respond_with_data("<p>noise</p>", content_type="text/html")
    keep, skip = httpserver.url_for("/keep"), httpserver.url_for("/skip")
    seeds = [Seed(url=keep), Seed(url=skip)]

    def llm(prompt: str) -> str:
        if "frontier links" in prompt:  # the seed-evaluation round: pick only /keep
            for line in prompt.splitlines():
                s = line.strip()
                if s[:1].isdigit() and "/keep" in s:
                    return f'[{s.split(".", 1)[0]}]'
            return "[]"
        return "[]"

    with WebClient() as wc:
        crawl = crawl_from_seeds(seeds, Brief(description="data"), wc=wc, llm=llm,
                                 rounds=1, browser=False)
    fetched = [(p.final_url or p.url) for p in crawl.pages]
    assert any(u.endswith("/keep") for u in fetched)  # the picked seed was fetched
    assert not any(u.endswith("/skip") for u in fetched)  # the rejected seed was NOT


def test_write_query_rejects_rows_whose_fields_are_all_empty(httpserver):
    # a query that matches the record container but whose FIELD selectors match nothing
    # yields all-empty rows -- that is not extraction, it is a guess. It must be rejected
    # (retried), not accepted as a success, and the hint must warn about client-rendered
    # / iframe / shadow-DOM content that a static query cannot reach.
    from webclient.pipelines.onboarding import write_query

    httpserver.expect_request("/p").respond_with_data(
        "<main>" + "".join(f'<div class="r"><span class="n">P{i}</span></div>' for i in range(3)) + "</main>",
        content_type="text/html",
    )
    replies = iter([
        'wq.doc.select_all(".r").extract(name=wq.doc.select(".missing").attr("text")).project()',  # empty
        'wq.doc.select_all(".r").extract(name=wq.doc.select(".n").attr("text")).project()',          # real
    ])
    prompts: list[str] = []

    def llm(prompt: str) -> str:
        prompts.append(prompt)
        return next(replies)

    with WebClient() as wc:
        art = write_query(httpserver.url_for("/p"), Brief(description="rows", fields=["name"]),
                          wc=wc, llm=llm, browser="never", retries=1)
    assert art is not None and art.row_count == 3  # only the real query is accepted
    assert all(r.get("name") for r in art.sample)
    assert len(prompts) == 2  # the all-empty query was rejected and retried
    assert "iframe or shadow DOM" in prompts[1] or "client-side" in prompts[1]  # content caveat


def test_write_query_rejects_a_missing_required_field(httpserver):
    # a query that fills some fields but leaves a REQUIRED one empty on every row is only a
    # partial guess -- reject + name the empty field. An OPTIONAL field left empty is fine.
    from webclient.pipelines.onboarding import write_query

    httpserver.expect_request("/p").respond_with_data(
        '<main><div class="r"><span class="n">A</span><span class="d">Jan 1</span></div></main>',
        content_type="text/html",
    )
    replies = iter([
        # date marked optional (to dodge the miss error) but its selector is wrong -> the
        # row is populated by title, yet the REQUIRED date is None on every row
        'wq.doc.select_all(".r").extract(title=wq.doc.select(".n").attr("text"), '
        'date=wq.doc.select(".nodate", optional=True).attr("text")).project()',
        'wq.doc.select_all(".r").extract(title=wq.doc.select(".n").attr("text"), '
        'date=wq.doc.select(".d").attr("text")).project()',        # date filled
    ])
    prompts: list[str] = []

    def llm(prompt: str) -> str:
        prompts.append(prompt)
        return next(replies)

    with WebClient() as wc:
        art = write_query(httpserver.url_for("/p"),
                          Brief(description="news", fields=["title", "date"]),
                          wc=wc, llm=llm, browser="never", retries=1)
    assert art is not None and art.row_count == 1 and art.sample[0]["date"] == "Jan 1"
    assert len(prompts) == 2 and '"date"' in prompts[1]  # the empty required field was named


def test_write_query_fallback_is_marked_incomplete_when_a_required_field_is_empty(httpserver):
    # if EVERY attempt leaves a required field empty, the returned artifact is kept as a
    # fallback but marked complete=False -- so the run is not reported ok (F3 regression).
    from webclient.pipelines.onboarding import write_query

    httpserver.expect_request("/p").respond_with_data(
        '<main><div class="r"><span class="n">A</span></div></main>', content_type="text/html")
    # date is required but its selector never matches (optional=True -> None on every row)
    code = ('wq.doc.select_all(".r").extract(title=wq.doc.select(".n").attr("text"), '
            'date=wq.doc.select(".nope", optional=True).attr("text")).project()')
    with WebClient() as wc:
        art = write_query(httpserver.url_for("/p"),
                          Brief(description="news", fields=["title", "date"]),
                          wc=wc, llm=lambda p: code, browser="never", retries=1)
    assert art is not None and art.row_count == 1  # a row came out (title populated)
    assert not art.complete  # ...but a required field is empty -> not complete -> run not ok


def test_parse_query_loads_written_code_and_falls_back_to_a_blob():
    # the model WRITES the query as a wq.doc chain; we eval it (load it as written). A
    # code fence / preamble is tolerated, a raw to_blob() blob is still accepted, and a
    # reply that is neither a wq chain nor a blob raises (so write_query retries).
    from webclient import wq
    from webclient.pipelines.onboarding import _parse_query

    code = 'wq.doc.select_all(".r").extract(n=wq.doc.select(".n").attr("text")).project()'
    want = "Document.select_all('.r').extract(n=Document.select('.n').attr('text')).project()"
    assert _parse_query(code).explain() == want
    assert _parse_query(f"here is the query:\n```python\n{code}\n```").explain() == want  # fenced + prose
    assert _parse_query("query = " + code).explain() == want  # leading assignment dropped
    blob = wq.doc.select_all(".r").extract(n=wq.doc.select(".n").attr("text")).project().to_blob()
    assert _parse_query(blob).explain() == want  # raw-blob fallback still works
    with pytest.raises(Exception):
        _parse_query("just some prose, not a query")


def test_parse_query_refuses_code_execution(tmp_path):
    # F1 regression: the loader drives our wq interface via a controlled AST walk, NOT eval,
    # so a prompt-injected line cannot reach __globals__/builtins and run code.
    from webclient.pipelines.onboarding import _parse_query

    marker = tmp_path / "pwned"
    payloads = [
        f"wq.reference.__globals__['__builtins__']['__import__']('os').system('touch {marker}')",
        f"wq.doc.select_all.__globals__['__builtins__']['open']('{marker}','w')",
        "wq.doc.select_all(__import__('os').getcwd())",
        "wq.doc.select(().__class__.__bases__[0].__subclasses__())",
    ]
    for p in payloads:
        with pytest.raises(Exception):
            _parse_query(p)
    assert not marker.exists()  # nothing executed -- refused before running


def test_zero_row_query_is_not_a_success(site):
    # a query that runs but extracts 0 rows -> ok is False with a clear reason (no more
    # "0 rows considered a success").
    products_url = site.url_for("/products")

    def llm(prompt: str) -> str:
        from webclient import wq
        if "web-search query" in prompt:
            return "acme products"
        if "frontier links" in prompt:
            for line in prompt.splitlines():
                s = line.strip()
                if s[:1].isdigit() and "/products" in s:
                    return f'[{s.split(".", 1)[0]}]'
            return "[]"
        if "crawled pages" in prompt:
            return json.dumps([{"url": products_url, "kind": "page", "tier": "must"}])
        if "Assess this page" in prompt:
            return json.dumps({"dataset_present": True, "is_queryable": True, "scrapability": 8, "verdict": "list"})
        if "query code" in prompt or "write a query" in prompt:
            # a query that selects a class that does not exist -> 0 rows
            return 'wq.doc.select_all(".does-not-exist").extract(x=wq.doc.attr("text")).project()'
        return "{}"

    def search(q, k):
        return [SearchHit(url=site.url_for("/"), title="Acme")]

    with WebClient() as wc:
        result = onboard_company("Acme", Brief(description="products"),
                                 wc=wc, llm=llm, search=search, browser=False)
    assert result.query is not None and result.query.row_count == 0
    assert not result.ok and result.reason == "authored query extracted 0 rows"
