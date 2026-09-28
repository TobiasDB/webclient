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
        'name=wq.doc.select(".name").attr("text"), '
        'price=wq.doc.select(".price").attr("text")).project()'
    )

    def search(query, k):  # a stub SearchFn: seed at the company home page
        # the query is deterministic: "<company> <brief.search>"
        assert query == "Acme products"
        return [SearchHit(url=site.url_for("/"), title="Acme", snippet="widgets")]

    def llm(prompt: str) -> str:  # a scripted model, routed by prompt content
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
            "Acme",
            Brief(description="the company's products", fields=["name", "price"], search="products"),
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


def test_start_url_seeds_the_crawl_directly_and_skips_web_search(site):
    # a brief that carries a start_url onboards from that page WITHOUT calling web search --
    # for a dataset that always lives at one known address (the "OR a start url" brief option).
    products_url = site.url_for("/products")
    code = ('wq.doc.select_all(".product").extract('
            'name=wq.doc.select(".name").attr("text"), price=wq.doc.select(".price").attr("text")).project()')

    def search(query, k):  # must NOT be called when start_url is set
        raise AssertionError("web search should be skipped when the brief has a start_url")

    def llm(prompt: str) -> str:
        if "frontier links" in prompt:
            return "[]"  # no navigation needed -- the start_url IS the dataset
        if "crawled pages" in prompt:
            return json.dumps([{"url": products_url, "kind": "page", "tier": "must", "note": "list"}])
        if "Assess this page" in prompt:
            return json.dumps({"dataset_present": True, "is_queryable": True, "completeness": "full",
                               "has_pagination": False, "scrapability": 9, "verdict": "products"})
        if "query code" in prompt or "write a query" in prompt:
            return code
        return "{}"

    with WebClient() as wc:
        result = onboard_company(
            "Acme",
            Brief(description="the products", fields=["name", "price"], start_url=products_url),
            wc=wc, llm=llm, search=search, browser=False,
        )
    assert result.ok, result.reason
    assert result.seeds == [products_url]  # seeded straight from the brief, no search
    assert result.evaluation is not None and result.evaluation.url == products_url
    assert result.query is not None and result.query.row_count == 3
    assert any("start_url" in s for s in result.steps)


def test_onboard_company_reports_when_no_seeds(site):
    def search(query, k):
        return []

    with WebClient() as wc:
        result = onboard_company(
            "Nobody", Brief(description="ghosts"), wc=wc,
            llm=lambda p: "{}", search=search, browser=False,
        )
    assert not result.ok and result.reason == "no search seeds"


def test_parse_query_rewrites_css_child_combinator_to_descendant():
    from webclient.pipelines.onboarding import _parse_query

    q = _parse_query('wq.doc.select_all("ul.list > li.item").extract('
                     'name=wq.doc.select("div.card > span.n").attr("text")).project()')
    ex = q.describe()
    assert ">" not in ex                                   # the strict child combinator is gone
    assert "ul.list li.item" in ex and "div.card span.n" in ex
    # XPath (which legitimately uses /) is left untouched
    q2 = _parse_query('wq.doc.select_all("//ul/li").extract(n=wq.doc.select(".n").attr("text")).project()')
    assert "//ul/li" in q2.describe()


def test_reference_prefers_the_page_over_an_xhr_unless_the_page_is_a_spa_shell():
    # a directly-scrapable page that ALSO fired an XHR must be referenced/queried as the PAGE,
    # not the XHR (the regression); an observed data API is used only for an SPA shell.
    from webclient.pipelines.onboarding import CandidateEval, _source_url, write_reference

    page = "https://site.com/products"
    api = "https://site.com/api/products.json"

    scrapable = CandidateEval(url=page, dataset_present=True, api_endpoint=api, flags={})  # no spa
    assert _source_url(scrapable) == page  # the page, NOT the XHR

    spa_shell = CandidateEval(url=page, dataset_present=True, api_endpoint=api,
                              flags={"spa": 0.9})  # a client-rendered shell backed by the API
    assert _source_url(spa_shell) == api

    with WebClient() as wc:
        assert str(write_reference(scrapable, wc=wc).url) == page


def test_flags_a_field_that_grabbed_a_whole_json_object(httpserver):
    # deep-query anomaly (found with the cheapest model on a 2nd-level resolve): a field resolved to
    # a JSON detail page but read the whole object -- stock = '{"count": 7}' instead of "7". The value
    # is non-empty, so the empty-field check misses it; a dedicated check flags it and the retry hint
    # tells the model to drill into the key. The query must NOT ship as complete.
    from webclient.pipelines.onboarding.query_diagnose import _blob_valued_fields, _looks_like_json_blob
    from webclient.pipelines.onboarding.query import write_query

    assert _looks_like_json_blob('{"count": 7}') and _looks_like_json_blob("[1, 2]")
    assert not _looks_like_json_blob("7") and not _looks_like_json_blob("SKU-2") and not _looks_like_json_blob("")

    brief = Brief(description="products", fields=["title", "stock"])
    # both forms of the container-grabbed anomaly: a stringified object, AND a real dict (.attr on JSON)
    assert _blob_valued_fields([{"title": "A", "stock": '{"count": 7}'}], brief) == ["stock"]
    assert _blob_valued_fields([{"title": "A", "stock": {"count": 7}}], brief) == ["stock"]
    # a list-valued LEAF of scalars is a valid multi-value field (tags), NOT a container
    tags_brief = Brief(description="quotes", fields=["text", "tags"])
    assert _blob_valued_fields([{"text": "q", "tags": ["a", "b"]}], tags_brief) == []
    # a field declared as a BRANCH (sub-fields) with a real nested dict is fine -- only its scalar leaves count
    branch_brief = Brief(description="p", fields=["title", "price.amount", "price.currency"])
    assert _blob_valued_fields([{"title": "x", "price": {"amount": "39", "currency": "USD"}}], branch_brief) == []

    # end-to-end: a query that reads the whole JSON object for `stock` is rejected + retried with the
    # drill-into-the-key hint, not accepted as complete.
    httpserver.expect_request("/it").respond_with_json({"stock": {"count": 7}, "sku": "SKU-9"})
    httpserver.expect_request("/list").respond_with_data(
        '<div class="card"><span class="t">Widget</span><a class="link" href="/it">view</a></div>',
        content_type="text/html")
    seen: list[str] = []

    def llm(prompt: str) -> str:
        seen.append(prompt)
        if len(seen) == 1:  # first: grab the whole object (the bug)
            return ('wq.doc.select_all("div.card").extract(title=wq.doc.select(".t").attr("text"),'
                    ' stock=wq.doc.select("a.link").attr("href").resolve().select("stock").attr("text")).project()')
        return ('wq.doc.select_all("div.card").extract(title=wq.doc.select(".t").attr("text"),'  # then drill in
                ' stock=wq.doc.select("a.link").attr("href").resolve().select("stock.count").attr("text")).project()')

    with WebClient() as wc:
        art = write_query(httpserver.url_for("/list"), Brief(description="products", fields=["title", "stock"]),
                          wc=wc, llm=llm, browser="never", retries=3)
    assert art is not None and art.complete and art.sample[0]["stock"] == "7"  # the drilled-in value
    assert len(seen) >= 2 and "DRILL INTO the key" in seen[1]  # the retry carried the targeted hint


def test_rejects_a_header_row_extracted_as_data(httpserver):
    # a <td>-built header row inside <tbody> gets extracted as a phantom {name:'Name', price:'Price'}
    # record (non-empty, so the empty-field check misses it). It must be rejected, not shipped as
    # complete; the retry hint points at .table() / a data-row selector.
    from webclient.pipelines.onboarding.query_diagnose import _first_row_is_header
    from webclient.pipelines.onboarding.query import write_query

    brief = Brief(description="products", fields=["name", "price"])
    assert _first_row_is_header([{"name": "Name", "price": "Price"}], brief)          # values == column names
    assert not _first_row_is_header([{"name": "Widget", "price": "10"}], brief)       # real data is not flagged

    httpserver.expect_request("/t").respond_with_data(
        "<table><tbody><tr><td>Name</td><td>Price</td></tr>"                          # header built from <td>
        "<tr><td>Widget</td><td>10</td></tr><tr><td>Cog</td><td>20</td></tr></tbody></table>",
        content_type="text/html")
    calls = {"n": 0}

    def llm(prompt: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:  # first: the naive selector that catches the header row
            return ('wq.doc.select_all("tbody tr").extract(name=wq.doc.select("td:nth-of-type(1)").attr("text"),'
                    ' price=wq.doc.select("td:nth-of-type(2)").attr("text")).project()')
        return ('wq.doc.select_all("tbody tr:not(:first-child)").extract('  # then: exclude the header row
                'name=wq.doc.select("td:nth-of-type(1)").attr("text"),'
                ' price=wq.doc.select("td:nth-of-type(2)").attr("text")).project()')

    with WebClient() as wc:
        art = write_query(httpserver.url_for("/t"), brief, wc=wc, llm=llm, browser="never", retries=3)
    assert art is not None and art.complete and art.row_count == 2       # the two real products, header gone
    assert art.sample[0]["name"] == "Widget"
    assert any("HEADER row" in a for a in art.attempts)                   # the header echo was diagnosed


def test_diagnoses_a_required_field_that_raises_on_some_rows(httpserver):
    # the Wikipedia bug: the record selector matches every row, the field selectors work on MOST
    # rows, but a leading summary/total row ("World") lacks the link the country rows have -- so a
    # NON-optional field selector raises and zeroes the WHOLE query. The old diagnostic said only
    # "0 rows (record selector matched N)", which misled the model into re-picking the record
    # selector; now it names the real fix: make the field optional.
    from webclient.pipelines.onboarding.query_diagnose import (
        _no_rows_hint, _short_fail_reason, _required_field_raises, _force_fields_optional, _test_query,
    )
    from webclient.pipelines.onboarding.artifacts import Brief

    rows_html = (
        '<tr><th>Rank</th><th>Country</th><th>Population</th></tr>'
        '<tr><td>–</td><td>World</td><td>8,000,000,000</td></tr>'          # summary row: NO country link
        '<tr><td>1</td><td><a title="India">India</a></td><td>1,400,000,000</td></tr>'
        '<tr><td>2</td><td><a title="China">China</a></td><td>1,410,000,000</td></tr>'
    )
    httpserver.expect_request("/pop").respond_with_data(
        f'<table class="wikitable"><tbody>{rows_html}</tbody></table>', content_type="text/html")

    with WebClient() as wc:
        doc = wc.fetch(httpserver.url_for("/pop"), browser="never")
        # the model's shape: country is NOT optional and misses on the "World" row -> the query raises
        expr = wq.doc.select_all("table.wikitable tbody tr:has(td)").extract(
            country=wq.doc.select("td a[title]").attr("text"),
            population=wq.doc.select("td:nth-of-type(3)").attr("text"),
        ).project()
        # the raise is detected, and forcing fields optional recovers real rows
        assert _required_field_raises(expr, doc) is not None
        opt = _force_fields_optional(expr)
        ok, orows = _test_query(opt, doc)
        assert ok and len([r for r in orows if r.get("country")]) == 2  # India + China (World has null country)
        # the diagnostics now point at the real fix, not the record selector
        hint = _no_rows_hint(expr, doc)
        assert "optional=True" in hint and "summary/total" in hint
        assert "make it optional" in _short_fail_reason(expr, [], Brief(description="d", fields=["country"]), doc)


def test_pagination_ignored_param_does_not_duplicate_rows(httpserver):
    # the LWN bug: the model bakes an offset pager (?offset=0,10,20,...) but the server IGNORES the
    # param on THIS url -- every "page" re-serves the SAME records under a slightly different URL
    # (the offset echoed in a canonical link), so a content-hash repeat check is fooled and the walk
    # emits the same rows on every page. Passing the RECORD selector to paginate makes the repeat be
    # judged by the records, so page two is a repeat -> the pager is not confirmed / the walk stops.
    from webclient.core.document.models import PagerHint, PaginationHint
    from webclient.pipelines.onboarding.query import _confirmed_mode, _mode_confirms
    from webclient.pipelines.onboarding.query_build import _executable_query
    from webclient.interface import wq

    def handler(req):  # every offset returns the SAME 3 records; only the canonical URL echoes offset
        from werkzeug.wrappers import Response
        off = req.args.get("offset", "0")
        rows = "".join(f'<article class="row"><span class="t">Item {i}</span></article>' for i in range(3))
        html = (f'<html><head><link rel="canonical" href="/list?offset={off}"></head>'
                f'<body><main>{rows}</main></body></html>')
        return Response(html, content_type="text/html")

    httpserver.expect_request("/list").respond_with_handler(handler)
    url = httpserver.url_for("/list")
    hint = PaginationHint(modes=[PagerHint(mode="pages", code="", param="offset", start=0, step=10)])

    with WebClient() as wc:
        doc = wc.fetch(url, browser="never")
        # the content-hash probe is FOOLED (offset echoed -> page 2 looks distinct); the record probe is not
        assert _mode_confirms(doc, hint.best, records="") is True             # fooled: bakes a bad pager
        assert _mode_confirms(doc, hint.best, records="article.row") is False  # records repeat -> not confirmed
        assert _confirmed_mode(doc, hint, records="article.row") is None       # so NO mode is confirmed
        # and even if the pager IS baked, the record dedup stops the walk -> no duplicate rows across offsets
        expr = wq.doc.select_all("article.row").extract(t=wq.doc.select(".t").attr("text")).project()
        exe = _executable_query(expr, url, None, paginate=True, mode=hint.best, max_pages=6)
        rows = exe.collect()
    assert len(rows) == 3, f"expected 3 unique rows, got {len(rows)} (duplicated across ignored offsets)"


def test_write_query_auto_repairs_a_near_miss_field_selector(httpserver):
    # the model writes an almost-correct query but mistypes a high-entropy class (widget vs
    # widgets); the pipeline swaps the mistyped class for the nearest real one in the record and
    # ships the REPAIRED query -- no wasted retry, no failed onboarding.
    from webclient.pipelines.onboarding import write_query

    httpserver.expect_request("/p").respond_with_data(
        "<main>" + "".join(
            f'<article class="product"><span class="widgets">W{i}</span>'
            f'<span class="price">{i}0</span></article>' for i in range(3)
        ) + "</main>",
        content_type="text/html",
    )

    def llm(prompt: str) -> str:  # one shot, with the near-miss ".widget"
        return ('wq.doc.select_all("article.product").extract('
                'name=wq.doc.select(".widget").attr("text"),'
                ' price=wq.doc.select(".price").attr("text")).project()')

    with WebClient() as wc:
        art = write_query(httpserver.url_for("/p"),
                          Brief(description="products", fields=["name", "price"]),
                          wc=wc, llm=llm, browser="never", retries=0)
    assert art is not None and art.complete and art.row_count == 3
    assert ".widgets" in art.describe            # the selector was repaired to the real class
    assert art.sample[0]["name"] == "W0"


def test_write_query_stops_early_when_a_required_field_is_absent(httpserver):
    # a required field the model can NEVER populate because it is genuinely NOT on the page: after a
    # couple of targeted retries the pipeline concludes the field is ABSENT, STOPS re-authoring it
    # (instead of burning every retry), and returns the best PARTIAL with the absent field named --
    # so onboarding fails FAST with a precise reason rather than looping on a field that isn't there.
    from webclient.pipelines.onboarding import write_query

    httpserver.expect_request("/p").respond_with_data(
        "<ul>" + "".join(
            f'<li class="post"><span class="title">T{i}</span><span class="loc">L{i}</span></li>'
            for i in range(3)
        ) + "</ul>",
        content_type="text/html",
    )
    calls = {"n": 0}

    def llm(prompt: str) -> str:  # always a VALID query, but 'team' lives nowhere on the page
        calls["n"] += 1
        sel = [".team", ".squad", ".group", ".unit", ".org"][min(calls["n"] - 1, 4)]
        return ('wq.doc.select_all("li.post").extract('
                'title=wq.doc.select(".title").attr("text"),'
                f' team=wq.doc.select("{sel}", optional=True).attr("text")).project()')

    with WebClient() as wc:
        art = write_query(httpserver.url_for("/p"),
                          Brief(description="posts", fields=["title", "team"]),
                          wc=wc, llm=llm, browser="never", retries=4)
    assert art is not None
    assert not art.complete and art.absent == ["team"]         # the absent field is named
    assert art.row_count == 3 and art.sample[0]["title"] == "T0"  # the partial keeps the other field
    assert calls["n"] == 2  # stopped after the 2nd attempt, NOT all 5 tries (retries=4 -> 5)


def test_write_query_uses_a_swappable_author_seam(httpserver, monkeypatch):
    # the authoring ENGINE is a swappable seam (_make_author): write_query owns the test/repair/
    # artifact orchestration and asks the Author only for the candidate query exprs. Phase 6 swaps
    # the text author for an index-loop author here. Inject a fake author that returns exprs with
    # NO llm and confirm write_query still produces a working artifact around it.
    from webclient.pipelines import onboarding
    from webclient.pipelines.onboarding import Author, _parse_query, write_query

    httpserver.expect_request("/p").respond_with_data(
        "<main>" + "".join(
            f'<article class="product"><span class="name">P{i}</span>'
            f'<span class="price">{i}9</span></article>' for i in range(3)
        ) + "</main>",
        content_type="text/html",
    )

    class FakeAuthor(Author):  # a non-LLM authoring engine
        def author(self, follow_up=None):
            return [_parse_query(
                'wq.doc.select_all("article.product").extract('
                'name=wq.doc.select(".name").attr("text"),'
                ' price=wq.doc.select(".price").attr("text")).project()'
            )]

    monkeypatch.setattr(onboarding.query, "_make_author", lambda *a, **k: FakeAuthor())  # the seam lives in the query stage module

    with WebClient() as wc:
        art = write_query(
            httpserver.url_for("/p"),
            Brief(description="products", fields=["name", "price"]),
            wc=wc, llm=lambda p: "", browser="never", retries=0,
        )
    assert art is not None and art.complete and art.row_count == 3
    assert art.sample[0] == {"name": "P0", "price": "09"}


def test_write_query_index_engine_picks_indexes_not_css(httpserver):
    # Phase 6: with author_engine="index" the model NEVER writes a selector -- it picks record +
    # field NUMBERS and build_query assembles the durable query. Here the "model" returns a JSON
    # index pick; write_query builds, tests, and ships a working artifact.
    from webclient.pipelines.onboarding import write_query

    httpserver.expect_request("/p").respond_with_data(
        "<main>" + "".join(
            f'<article class="product"><span class="name">P{i}</span>'
            f'<span class="price">{i}9</span></article>' for i in range(3)
        ) + "</main>",
        content_type="text/html",
    )

    calls = {"n": 0}

    def llm(prompt: str) -> str:  # the index policy asks for a JSON pick BY NUMBER (never a selector)
        calls["n"] += 1
        assert "PICKING NUMBERS" in prompt or "record" in prompt  # it's the index prompt, not code
        # R1 = the article.product region; F1 = name leaf, F2 = price leaf
        return '{"record": 1, "fields": {"name": 1, "price": 2}, "done": true}'

    with WebClient() as wc:
        art = write_query(
            httpserver.url_for("/p"),
            Brief(description="products", fields=["name", "price"]),
            wc=wc, llm=llm, browser="never", retries=1, author_engine="index",
        )
    assert calls["n"] >= 1                       # the index policy was consulted
    assert art is not None and art.complete and art.row_count == 3
    assert art.sample[0] == {"name": "P0", "price": "09"}
    assert "select_all" in art.describe          # a normal, durable query came out the other side


def test_index_engine_falls_back_to_text_when_detection_fails(httpserver):
    # record detection is a HINT, not a requirement: on a page whose records don't form a
    # detectable region (only 2 -> below min_items), author_engine="index" degrades to the TEXT
    # author rather than failing. The index policy prompt is never even reached (no hint).
    from webclient.pipelines.onboarding import write_query

    httpserver.expect_request("/p").respond_with_data(
        '<main><article class="product"><span class="name">A</span></article>'
        '<article class="product"><span class="name">B</span></article></main>',
        content_type="text/html",
    )

    seen = {"index": 0, "text": 0}

    def llm(prompt: str) -> str:
        if "PICKING NUMBERS" in prompt:  # the index policy prompt -- should NOT be reached (no hint)
            seen["index"] += 1
            return "{}"
        seen["text"] += 1  # the text author's code-authoring prompt
        return 'wq.doc.select_all("article.product").extract(name=wq.doc.select(".name").attr("text")).project()'

    with WebClient() as wc:
        art = write_query(
            httpserver.url_for("/p"), Brief(description="products", fields=["name"]),
            wc=wc, llm=llm, browser="never", retries=1, author_engine="index",
        )
    assert art is not None and art.complete and art.row_count == 2  # the fallback authored a working query
    assert seen["text"] >= 1 and seen["index"] == 0  # text did the work; index policy never ran


def test_write_query_paginates_a_paginated_source(httpserver):
    # paginated=True: the model authors a ONE-PAGE query; the pipeline bakes .paginate(by="link")
    # into the shipped blob, so run_query pulls the WHOLE dataset (the page-1-only bug, fixed).
    from webclient.pipelines.onboarding import run_query, write_query

    httpserver.expect_request("/p1").respond_with_data(
        '<main><article class="r"><span class="n">A</span></article>'
        '<article class="r"><span class="n">B</span></article></main>'
        '<a rel="next" href="/p2">next</a>',
        content_type="text/html",
    )
    httpserver.expect_request("/p2").respond_with_data(
        '<main><article class="r"><span class="n">C</span></article></main>',  # no next
        content_type="text/html",
    )

    def llm(prompt: str) -> str:  # a normal single-page extraction (no 'next' field)
        return 'wq.doc.select_all("article.r").extract(n=wq.doc.select(".n").attr("text")).project()'

    with WebClient() as wc:
        art = write_query(
            httpserver.url_for("/p1"), Brief(description="items", fields=["n"]),
            wc=wc, llm=llm, browser="never", retries=0, paginated=True,
        )
        assert art is not None and ".paginate(" in art.describe  # pagination baked into the blob
        assert art.row_count == 2  # authoring TESTED page one only (fast)
        # a paginated source gets BOTH queries: B/all (this artifact, pages walked) and A/latest.
        assert art.mode == "all" and art.covers_all
        assert art.latest is not None and art.latest.mode == "latest"
        assert ".paginate(" not in art.latest.describe  # A is page one -- no backfill pager
        rows = run_query(art, wc=wc)  # the shipped query (B) walks every page
        latest_rows = run_query(art.latest, wc=wc)  # A pulls just page one (the newest)
    assert [r["n"] for r in rows] == ["A", "B", "C"]
    assert [r["n"] for r in latest_rows] == ["A", "B"]


def test_write_query_bakes_param_advance_from_the_hint(httpserver):
    # a computed (?page=N) source with NO rel=next link -- only a pagination widget. The hint's best
    # mode (pages="page") makes write_query bake .paginate(pages="page", ...), so run_query walks
    # ?page=1,2. (A next-link default could not reach page 2 here.)
    from webclient.pipelines.onboarding import run_query, write_query
    from webclient.core.document.models import PagerHint, PaginationHint

    def _page(rows):
        arts = "".join(f'<article class="r"><span class="n">{n}</span></article>' for n in rows)
        return f'<main>{arts}</main><nav class="pagination">pages</nav>'  # widget, but no rel=next

    httpserver.expect_request("/list", query_string="page=1").respond_with_data(_page(["A", "B"]), content_type="text/html")
    httpserver.expect_request("/list", query_string="page=2").respond_with_data(_page(["C"]), content_type="text/html")
    httpserver.expect_request("/list", query_string="page=3").respond_with_data("", status=404)

    def llm(prompt: str) -> str:
        return 'wq.doc.select_all("article.r").extract(n=wq.doc.select(".n").attr("text")).project()'

    with WebClient() as wc:
        art = write_query(
            httpserver.url_for("/list") + "?page=1", Brief(description="items", fields=["n"]),
            wc=wc, llm=llm, browser="never", retries=0, paginated=True,
            pagination_hint=PaginationHint(modes=[PagerHint(mode="pages", param="page", start=1, code='.paginate(pages="page")')]),
        )
        assert art is not None and "pages=" in art.describe  # the page-param pager was baked, not next=
        rows = run_query(art, wc=wc)
    assert [r["n"] for r in rows] == ["A", "B", "C"]  # walked ?page=1,2 then stopped at the 404


def test_write_query_probe_downgrades_a_fake_pager(httpserver):
    # paginated=True but there is NO real second page (a pagination widget, no rel=next, no working
    # page param). The probe walks page two, finds nothing distinct, and ships page one only -- no
    # .paginate() baked, so run_query never pages into an empty/duplicate page.
    from webclient.pipelines.onboarding import write_query

    httpserver.expect_request("/only").respond_with_data(
        '<main><article class="r"><span class="n">A</span></article>'
        '<article class="r"><span class="n">B</span></article></main>'
        '<nav class="pagination">1</nav>',  # a widget, but no rel=next / page-param link
        content_type="text/html",
    )

    def llm(prompt: str) -> str:
        return 'wq.doc.select_all("article.r").extract(n=wq.doc.select(".n").attr("text")).project()'

    with WebClient() as wc:
        art = write_query(
            httpserver.url_for("/only"), Brief(description="items", fields=["n"]),
            wc=wc, llm=llm, browser="never", retries=0, paginated=True,
        )
    assert art is not None
    assert ".paginate(" not in art.describe  # the probe found no page two -> pager not baked
    assert art.row_count == 2  # ships page one's rows honestly


def test_sample_table_collapses_newlines_so_columns_dont_shift():
    # an output-summary bug: a value with a newline (an RSS description) broke the aligned
    # sample table so LATER columns rendered shifted/empty. Cells now collapse whitespace.
    from webclient.pipelines.onboarding import _cell, _render_table

    assert _cell("line1\nline2\twith  spaces") == "line1 line2 with spaces"
    rows = [{"title": "A", "desc": "multi\nline\ndesc", "link": "https://x/a", "cat": "News"}]
    body = _render_table(rows)[-1]  # the single data row
    assert "\n" not in body                              # the row is a SINGLE line
    assert all(v in body for v in ("A", "multi line desc", "https://x/a", "News"))  # nothing shifted/empty


def test_write_query_keeps_the_page_in_context_across_retries(httpserver):
    # a conversation-capable llm gets the PAGE (guide + skeleton) once as the opening turn; each
    # retry is a short follow-up, so the page is not re-submitted every attempt.
    from webclient.pipelines.onboarding import write_query

    httpserver.expect_request("/p").respond_with_data(
        "<main>" + "".join(f'<div class="r"><span class="n">P{i}</span></div>' for i in range(3)) + "</main>",
        content_type="text/html",
    )
    turns: list[str] = []
    replies = iter([
        'wq.doc.select_all(".r")',  # 1st: no project -> 0 data rows, triggers a retry
        'wq.doc.select_all(".r").extract(name=wq.doc.select(".n").attr("text")).project()',
    ])

    class _Chat:
        def send(self, text: str) -> str:
            turns.append(text)
            return next(replies)

    class _ChatLLM:  # a conversation-capable model (like LlmClient)
        def conversation(self):
            return _Chat()

        def __call__(self, prompt: str) -> str:  # pragma: no cover - fallback, unused here
            turns.append(prompt)
            return next(replies)

    with WebClient() as wc:
        art = write_query(httpserver.url_for("/p"), Brief(description="rows", fields=["name"]),
                          wc=wc, llm=_ChatLLM(), browser="never", retries=2)
    assert art is not None and art.complete and art.row_count == 3
    assert len(turns) == 2
    # the opening carries the whole page + guide; the retry is a short follow-up (no guide, no
    # full skeleton) -- the page stays in the conversation instead of being re-submitted.
    assert "Reading a value from an ATTRIBUTE" in turns[0]  # the guide is in the opening
    assert "Reading a value from an ATTRIBUTE" not in turns[1]  # ...but NOT re-sent on the retry
    assert "did not extract" in turns[1] and len(turns[1]) < len(turns[0]) // 2


def test_write_query_rejects_resolve_on_a_value_and_records_the_attempt(httpserver):
    # the model sometimes calls .resolve() on .attr("text") (a value, not a link). It's caught
    # with a targeted message, and the rejection is recorded on the returned artifact's trail.
    httpserver.expect_request("/p").respond_with_data(
        '<main><div class="r"><a href="/d/1">Item</a></div></main>', content_type="text/html",
    )
    follow_ups: list[str] = []
    replies = iter([
        # 1st: resolves the TEXT (invalid) -> rejected with the resolve feedback
        'wq.doc.select_all(".r").extract(name=wq.doc.select("a").attr("text").resolve()'
        '.select(".x").attr("text")).project()',
        # 2nd: a plain valid query
        'wq.doc.select_all(".r").extract(name=wq.doc.select("a").attr("text")).project()',
    ])

    class _Chat:
        def send(self, text: str) -> str:
            follow_ups.append(text)
            return next(replies)

    class _ChatLLM:
        def conversation(self) -> "_Chat":
            return _Chat()

    from webclient.pipelines.onboarding import write_query

    with WebClient() as wc:
        art = write_query(httpserver.url_for("/p"), Brief(description="rows", fields=["name"]),
                          wc=wc, llm=_ChatLLM(), browser="never", retries=2)
    assert art is not None and art.complete
    assert ".resolve() follows a LINK" in follow_ups[1]  # the targeted feedback was sent
    assert art.attempts and "resolve() called on a value" in art.attempts[0]  # recorded in the trail


def test_recency_guidance_feeds_the_evaluator_read_into_the_query_prompt():
    # evaluate_candidate identifies the sort order + where the most recent records are;
    # that hint is injected into the query-writing prompt to assist the first attempt.
    from webclient.pipelines.onboarding import CandidateEval, _query_prompt, _recency_guidance

    ev = CandidateEval(url="http://x", sort_order="newest-first",
                       recency_hint="the latest is in the '2026' tab")
    guidance = _recency_guidance(ev)
    assert "newest-first" in guidance and "2026" in guidance and "MOST RECENT" in guidance
    prompt = _query_prompt(Brief(description="news", fields=["title"]), "SKEL", recency=guidance)
    assert "RECENCY (from the page evaluation)" in prompt and "2026" in prompt
    # a non-dated dataset -> no recency noise in the prompt
    assert _recency_guidance(CandidateEval(url="http://x")) == ""


def test_brief_hints_are_passed_down_into_the_query_prompt():
    # brief-specific STRUCTURAL guidance (e.g. ir-events' upcoming/archived split, the single
    # differently-formatted upcoming row) reaches the query author; a brief with no hints adds none.
    from webclient.pipelines.onboarding import _query_prompt

    brief = Brief(description="events", fields=["title"],
                  hints="UPCOMING is a single differently-formatted row; ARCHIVED is tabbed by year.")
    prompt = _query_prompt(brief, "SKEL")
    assert "DATASET NOTES (from the brief" in prompt
    assert "single differently-formatted row" in prompt
    # no hints -> no injected notes (the static split-query paragraph still references DATASET
    # NOTES conditionally, but the brief's own note block is absent)
    assert "DATASET NOTES (from the brief" not in _query_prompt(
        Brief(description="events", fields=["title"]), "SKEL")


def test_write_query_retries_for_recent_data_when_the_first_query_is_stale(httpserver):
    # the hidden-tabs failure: the model first selects an ARCHIVED tab (complete but stale);
    # the writer nudges it for the MOST RECENT data, and it lands on the current tab.
    from datetime import date, timedelta

    from webclient.pipelines.onboarding import write_query

    today = date.today()
    old = [(today - timedelta(days=760 + i * 30)).isoformat() for i in range(3)]  # ~2 yrs, monthly
    new = [(today - timedelta(days=i * 7)).isoformat() for i in range(3)]         # recent, weekly
    page = (
        "<main>"
        + '<section class="archive">'
        + "".join(f'<div class="item"><span class="t">Old {i}</span><time>{old[i]}</time></div>'
                  for i in range(3)) + "</section>"
        + '<section class="current">'
        + "".join(f'<div class="item"><span class="t">New {i}</span><time>{new[i]}</time></div>'
                  for i in range(3)) + "</section></main>"
    )
    httpserver.expect_request("/p").respond_with_data(page, content_type="text/html")
    replies = iter([
        # 1st: the ARCHIVED tab -> complete but STALE
        'wq.doc.select_all(".archive .item").extract('
        'title=wq.doc.select(".t").attr("text"), date=wq.doc.select("time").attr("text")).project()',
        # 2nd (after the recency nudge): the CURRENT tab -> fresh
        'wq.doc.select_all(".current .item").extract('
        'title=wq.doc.select(".t").attr("text"), date=wq.doc.select("time").attr("text")).project()',
    ])
    follow_ups: list[str] = []

    class _Chat:
        def send(self, text: str) -> str:
            follow_ups.append(text)
            return next(replies)

    class _ChatLLM:
        def conversation(self) -> "_Chat":
            return _Chat()

    with WebClient() as wc:
        art = write_query(httpserver.url_for("/p"), Brief(description="news", fields=["title", "date"]),
                          wc=wc, llm=_ChatLLM(), browser="never", retries=2)
    assert art is not None and art.complete and not art.stale  # retried onto the fresh data
    assert "New" in str(art.sample) and "Old" not in str(art.sample)  # the current tab
    assert len(follow_ups) == 2  # opening + one recency retry
    assert "recent" in follow_ups[1].lower() and "archived" in follow_ups[1].lower()


def test_parse_queries_splits_sections_on_the_dashes_delimiter():
    # a split reply is one wq.doc chain PER section, separated by a line of only dashes; a
    # single reply (and a raw blob) still yields exactly one query -- the unchanged path.
    from webclient.pipelines.onboarding import _parse_queries, _split_queries

    two = ('wq.doc.select_all(".up .c").extract(title=wq.doc.select(".t").attr("text")).project()\n'
           '---\n'
           'wq.doc.select_all(".past .row").extract(title=wq.doc.select("h3").attr("text")).project()')
    assert len(_split_queries(two)) == 2 and len(_parse_queries(two)) == 2
    one = 'wq.doc.select_all(".r").extract(title=wq.doc.select(".t").attr("text")).project()'
    assert len(_parse_queries(one)) == 1
    # a stray delimiter / fences don't create empty queries
    fenced = f"```python\n{one}\n```"
    assert len(_parse_queries(fenced)) == 1


# a page whose dataset is SPLIT across two differently-shaped sections: a single "upcoming"
# callout (its own markup) above a "past" list of rows -- the ir-events shape.
_SPLIT_PAGE = (
    '<main>'
    '<section class="upcoming"><div class="callout"><h2 class="hl">Q4 Earnings Call</h2></div></section>'
    '<section class="past">'
    '<div class="row"><h3>Q3 Earnings</h3></div>'
    '<div class="row"><h3>Q2 Earnings</h3></div>'
    '<div class="row"><h3>Q1 Earnings</h3></div>'
    '</section></main>'
)
_SPLIT_REPLY = (
    'wq.doc.select_all(".upcoming .callout").extract(title=wq.doc.select(".hl").attr("text")).project()\n'
    '---\n'
    'wq.doc.select_all(".past .row").extract(title=wq.doc.select("h3").attr("text")).project()'
)


def test_write_query_combines_two_sections_into_one_dataset(httpserver):
    # the model writes ONE simple query per section (separated by ---); the pipeline tests each,
    # concatenates the rows into one flat dataset, and run_query reproduces the union.
    from webclient.pipelines.onboarding import run_query, write_query

    httpserver.expect_request("/events").respond_with_data(_SPLIT_PAGE, content_type="text/html")

    with WebClient() as wc:
        art = write_query(httpserver.url_for("/events"),
                          Brief(description="events", fields=["title"]),
                          wc=wc, llm=lambda p: _SPLIT_REPLY, browser="never", retries=0)
        assert art is not None and art.complete
        assert len(art.parts) == 2 and art.row_count == 4  # 1 upcoming + 3 past, concatenated
        titles = {r["title"] for r in art.sample}
        assert "Q4 Earnings Call" in titles and "Q3 Earnings" in titles  # both shapes present
        rows = run_query(art, wc=wc)  # the shipped artifact re-runs each section and concatenates
    assert {r["title"] for r in rows} == {
        "Q4 Earnings Call", "Q3 Earnings", "Q2 Earnings", "Q1 Earnings"
    }


def test_write_query_tolerates_an_empty_split_section(httpserver):
    # the UPCOMING section is empty (no callout) -- its section query matches 0 rows. The split
    # query is still COMPLETE on the union (the past rows), the empty section contributes nothing,
    # and it is nudged exactly once before being accepted.
    from webclient.pipelines.onboarding import write_query

    empty_upcoming = _SPLIT_PAGE.replace('<div class="callout"><h2 class="hl">Q4 Earnings Call</h2></div>', "")
    httpserver.expect_request("/events").respond_with_data(empty_upcoming, content_type="text/html")

    sent: list[str] = []

    def llm(prompt: str) -> str:
        sent.append(prompt)
        return _SPLIT_REPLY  # same reply each turn (a genuinely-empty upcoming section)

    with WebClient() as wc:
        art = write_query(httpserver.url_for("/events"),
                          Brief(description="events", fields=["title"]),
                          wc=wc, llm=llm, browser="never", retries=2)
    assert art is not None and art.complete
    assert len(art.parts) == 2 and art.row_count == 3  # only the past rows; upcoming is empty
    assert art.parts[0].row_count == 0  # the empty section, kept
    assert len(sent) == 2  # opening + exactly one empty-section nudge, then accepted
    assert any("0 records" in a for a in art.attempts)  # the nudge is recorded in the trail


def test_write_query_single_section_has_no_parts(httpserver):
    # a plain (non-split) reply behaves exactly as before: one query, no parts recorded.
    from webclient.pipelines.onboarding import write_query

    httpserver.expect_request("/events").respond_with_data(_SPLIT_PAGE, content_type="text/html")
    single = 'wq.doc.select_all(".past .row").extract(title=wq.doc.select("h3").attr("text")).project()'
    with WebClient() as wc:
        art = write_query(httpserver.url_for("/events"),
                          Brief(description="events", fields=["title"]),
                          wc=wc, llm=lambda p: single, browser="never", retries=0)
    assert art is not None and art.complete and art.parts == [] and art.row_count == 3


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


def test_parse_date_delegates_to_dateutil_across_formats_and_locales():
    # date parsing is delegated to python-dateutil (not a hand-rolled format list): ISO, RFC 822,
    # month names (both orders / abbreviated), and localised forms all parse. The DOT vs SLASH
    # separator disambiguates day/month order (European dot = day-first, US slash = month-first).
    import datetime

    from webclient.pipelines.onboarding import _parse_date, _timeliness

    D = datetime.date
    assert _parse_date("2025-12-18") == D(2025, 12, 18)                 # ISO
    assert _parse_date("Tue, 09 Sep 2026 13:00:00 GMT") == D(2026, 9, 9)  # RFC 822 (RSS pubDate)
    assert _parse_date("December 18, 2025") == D(2025, 12, 18)          # month name
    assert _parse_date("18 Dec 2025") == D(2025, 12, 18)               # day-first month name
    assert _parse_date("Sept. 5, 2026") == D(2026, 9, 5)               # abbreviated with a dot
    assert _parse_date("31.12.2026") == D(2026, 12, 31)                # EU dot
    assert _parse_date("01.03.2026") == D(2026, 3, 1)                  # EU dot, AMBIGUOUS -> day-first (1 Mar)
    assert _parse_date("12/18/2025") == D(2025, 12, 18)               # US slash -> month-first
    # a non-date / bare number is refused (not coerced to today), so a stray value isn't a date
    assert _parse_date("5") is None and _parse_date("TBA") is None and _parse_date("2 days ago") is None

    news = Brief(description="ir news", fields=["title", "date"])
    today = datetime.date.today()

    def de(days_ago: int) -> str:  # DE-dot dates now drive timeliness (they parse via dateutil)
        return (today - datetime.timedelta(days=days_ago)).strftime("%d.%m.%Y")

    assert not _timeliness([{"date": de(2)}, {"date": de(12)}, {"date": de(22)}], news)[1]  # recent -> timely
    assert _timeliness([{"date": de(200)}, {"date": de(210)}, {"date": de(220)}], news)[1]  # old -> STALE


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

    queries: list[str] = []

    def search(query, k):
        queries.append(query)
        if len(queries) == 1:  # the first query pulls the look-alike company
            return [SearchHit(url="https://squareup.com/", title="Square")]
        return [SearchHit(url="https://squarepoint.com/", title="Squarepoint Capital")]

    def llm(prompt):
        if "belong" in prompt:  # verify: the right one belongs only in the second set
            return '{"belong": [0]}' if "https://squarepoint.com" in prompt else '{"belong": []}'
        return "{}"

    brief = Brief(description="ir news", search="capital", look=["hedge fund"])
    seeds = search_web(brief, "Squarepoint", search=search, llm=llm)
    assert len(queries) == 2 and queries[0] != queries[1]  # retried with a stricter query
    assert queries[0] == "Squarepoint capital"  # deterministic: company + the brief's qualifier
    assert [s.url for s in seeds] == ["https://squarepoint.com/"]


def test_search_web_broadens_and_retries_on_no_results():
    # NO results (empty, or a backend error) -> retry with a BROADER/different query, not the
    # narrowing disambiguation.
    from webclient.pipelines.onboarding import search_web

    queries: list[str] = []

    def search(query, k):
        queries.append(query)
        return [] if len(queries) == 1 else [SearchHit(url="https://acme.com/blog", title="Acme")]

    def llm(prompt):
        if "belong" in prompt:
            return '{"belong": [0]}'
        return "{}"

    brief = Brief(description="blog", look=["the blog"], search="press releases")
    seeds = search_web(brief, "Acme", search=search, llm=llm)
    assert queries[0] == "Acme press releases"  # deterministic: company + the brief's qualifier
    assert len(queries) == 2 and queries[0] != queries[1]  # a BROADER term on retry
    assert [s.url for s in seeds] == ["https://acme.com/blog"]


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
    # a browser-only source (a static UA is BLOCKED with no SPA/anti-bot flag, e.g. Wikipedia 403):
    # the browser tier is baked in even with no render flag, so the shipped blob re-fetches via the
    # browser instead of statically (which would 403 -> 0 rows).
    assert write_resolve([]).browser is None
    only_browser = write_resolve([], needs_browser=True)
    assert only_browser.browser is not None and only_browser.browser.when == "always"


def test_onboarding_fetch_is_a_thin_passthrough_escalation_lives_in_the_auto_ladder():
    # the browser-only-site (Wikipedia / investor.nvidia.com 403) escalation is done ONCE, by the
    # core `auto` resolve ladder (webclient.core.client.resolve_loop._BLOCK_STATUSES) that EVERY fetch
    # -- a plain fetch, the crawl, onboarding -- shares. So onboarding's _fetch must NOT hand-roll its
    # own retry: it delegates a single fetch with the given tier. (The escalation itself is covered by
    # test_fetch.py::test_auto_escalates_a_blocking_403... and test_crawl.py's crawl escalation test.)
    from webclient.pipelines.onboarding.common import _fetch

    class _Doc:
        ok = True

    class _WC:
        def __init__(self): self.calls = []
        def fetch(self, url, *, browser, **kw):
            self.calls.append(browser)
            return _Doc()

    wc = _WC()
    _fetch(wc, "http://x/", "auto", optional=True)
    assert wc.calls == ["auto"]  # exactly one fetch -- no second "always" retry hand-rolled here


def test_candidate_scoring_prefers_the_listing_over_a_queryable_drilldown():
    # a listing whose rows link to per-item JSON endpoints (/api/{id}): the crawl surfaces those
    # endpoints as candidates, and one -- a SINGLE record -- can look "queryable". The model tiers the
    # listing MUST and the per-record endpoint SHOULD; the listing must WIN, so a drill-down endpoint
    # that merely happens to be queryable isn't mis-chosen as the dataset (which yields 1 row, not all).
    from webclient.pipelines.onboarding.evaluate import _candidate_score
    from webclient.pipelines.onboarding import CandidateEval

    listing = CandidateEval(url="/list", dataset_present=True, is_queryable=False, scrapability=6)
    drilldown = CandidateEval(url="/api/1", dataset_present=True, is_queryable=True, scrapability=8)
    # tier dominates: the MUST listing beats a SHOULD queryable drill-down with higher scrapability
    assert _candidate_score(listing, "must") > _candidate_score(drilldown, "should")
    # within the SAME tier, a queryable source is still preferred, and scrapability breaks ties
    assert _candidate_score(drilldown, "must") > _candidate_score(listing, "must")
    # a source without the dataset is never preferred over one that has it, regardless of tier
    empty = CandidateEval(url="/x", dataset_present=False, is_queryable=True, scrapability=9)
    assert _candidate_score(empty, "must") < _candidate_score(listing, "could")


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


def test_evaluate_reports_a_model_outage_distinctly_from_an_empty_source(httpserver):
    # a transient MODEL outage (rate limit / quota -> LlmError on every call) must NOT be reported as
    # "this source has no dataset". The eval is flagged llm_unavailable so the run says RETRY, not
    # "no usable source found" -- otherwise a throttled model looks like a dead site.
    from webclient.llm.client import LlmError

    httpserver.expect_request("/list").respond_with_data(
        "<main>" + "".join(f'<article class="row"><span class="t">R{i}</span></article>' for i in range(5))
        + "</main>", content_type="text/html",
    )

    def throttled(prompt):  # the shim/API is rate-limited: every call errors
        raise LlmError(1, "rate limited")

    with WebClient() as wc:
        ev = evaluate_candidate(Candidate(url=httpserver.url_for("/list")),
                                Brief(description="rows", fields=["t"]), wc=wc, llm=throttled, browser="never")
    assert ev.llm_unavailable and not ev.dataset_present  # the page was NOT judged empty -- it was never assessed
    assert "unavailable" in ev.verdict
    # and the whole pipeline: a throttled evaluate ends with a RETRY reason, not "no usable source found"
    with WebClient() as wc2:
        result = onboard_company(
            "Co", Brief(description="rows", fields=["t"]), wc=wc2, llm=throttled,
            search=lambda q, k: [SearchHit(url=httpserver.url_for("/list"), title="Co", snippet="")],
            browser=False,
        )
    assert not result.ok and "unavailable" in result.reason and "no usable source" not in result.reason


def test_evaluate_honours_a_brief_exit_condition(httpserver):
    # a brief-level exit_when is passed to the evaluator; when the model says it holds, the
    # eval carries exit_when_met so the pipeline can stop cleanly (never authoring a query).
    httpserver.expect_request("/events").respond_with_data(
        "<main><section class='past'><div class='item'>Old event</div></section>"
        "<section class='upcoming'></section></main>",  # upcoming is EMPTY
        content_type="text/html",
    )
    prompts: list[str] = []

    def llm(prompt: str) -> str:
        prompts.append(prompt)
        return ('{"dataset_present": true, "scrapability": 6, '
                '"exit_when_met": true, "exit_reason": "the upcoming section is empty", '
                '"verdict": "events page; no upcoming"}')

    brief = Brief(description="investor events", fields=["title", "date"],
                  exit_when="the upcoming-events section is empty")
    with WebClient() as wc:
        ev = evaluate_candidate(Candidate(url=httpserver.url_for("/events")), brief,
                                wc=wc, llm=llm, browser="never")
    assert ev.exit_when_met and "empty" in ev.exit_reason
    assert "EXIT CONDITION" in prompts[0]  # the condition reached the model

    # a brief with NO exit condition never honours a stray exit_when_met from the model
    with WebClient() as wc:
        ev2 = evaluate_candidate(Candidate(url=httpserver.url_for("/events")),
                                 Brief(description="events", fields=["title"]),
                                 wc=wc, llm=llm, browser="never")
    assert ev2.exit_when_met is False


def test_exit_condition_reviews_the_query_result_and_flags_a_likely_miss():
    # the brief's exit_when is re-checked against the query RESULT (not just the page): when it
    # holds because records were probably SKIPPED, it is flagged; a genuine clean exit is noted
    # but not flagged; with no exit_when the review is skipped entirely.
    from webclient.pipelines.onboarding import (
        OnboardingResult,
        QueryArtifact,
        _RunArtifacts,
        review_query_exit,
    )

    brief = Brief(description="investor events", fields=["title", "date"],
                  exit_when="the upcoming-events section is empty")
    art = QueryArtifact(blob="b", describe="wq.doc...", row_count=3,
                        sample=[{"title": "Q1 FY25 Earnings", "date": "2024-03-01"}])
    result = OnboardingResult(company="Acme", brief=brief, query=art)

    prompts: list[str] = []

    def miss_llm(prompt: str) -> str:
        prompts.append(prompt)
        return ('{"met": true, "likely_missed": true, '
                '"reason": "no upcoming rows in the result — the single upcoming callout was likely missed"}')

    rev = review_query_exit(result, _RunArtifacts(), brief, llm=miss_llm)
    assert rev is not None and rev.stage == "exit"
    assert rev.verdict == "likely miss" and rev.passed is False  # flagged for the human
    assert rev.issues and "MISSED" in rev.issues[0]
    assert "EXIT CONDITION" in prompts[0] and "RESULT" in prompts[0]

    # a genuine clean exit (condition holds, nothing missed) is NOTED but not flagged
    clean = review_query_exit(result, _RunArtifacts(), brief,
                              llm=lambda p: '{"met": true, "likely_missed": false, "reason": "no upcoming events scheduled"}')
    assert clean is not None and clean.verdict == "met" and clean.passed is True

    # no exit_when on the brief -> the review is skipped
    plain = OnboardingResult(company="Acme", brief=Brief(description="events"), query=art)
    assert review_query_exit(plain, _RunArtifacts(), Brief(description="events"),
                             llm=lambda p: "{}") is None


# --------------------------------------------------------------------------- #
# Prompts-as-data: the templates load and render with the right variables.
# --------------------------------------------------------------------------- #


def test_prompt_templates_load_and_render():
    # every prompt file loads and renders; the routing substrings the pipeline (and
    # the scripted stub llm) rely on survive the move to data.
    assert "frontier links" in render_prompt(
        "pick_edges", company="Acme", description="d", fields_line="", listing="0. http://x"
    )
    assert "crawled pages" in render_prompt(
        "select_candidates", description="d", fields_line="", pages_json="[]"
    )
    ev = render_prompt(
        "evaluate_candidate", description="d", fields_line=" Target fields: a.",
        candidate_url="http://c", flag_map_json="{}", endpoints_json="[]", skeleton="SKEL",
        exit_condition="",
    )
    assert "Assess this page" in ev and "http://c" in ev and "SKEL" in ev
    assert "exit_when_met" in ev  # the exit-condition key is always in the JSON schema
    wq_prompt = render_prompt(
        "write_query", guide="GUIDE-TEXT", description="d", fields_line="",
        pager="", skeleton="SKEL", hints="", recency="",
    )
    assert "query syntax" in wq_prompt and "query code" in wq_prompt
    assert "MOST RECENT" in wq_prompt  # the recency / hidden-tabs guidance is in the prompt
    assert "SPLIT DATASETS" in wq_prompt and "---" in wq_prompt  # one query per section, joined by the pipeline
    assert wq_prompt.startswith("GUIDE-TEXT")
    # the exit-condition-against-the-RESULT review prompt renders with its placeholders
    exit_prompt = render_prompt(
        "review_query_exit", description="d", exit_condition="upcoming is empty",
        row_count="3", sample="[]",
    )
    assert "EXIT CONDITION" in exit_prompt and "RESULT" in exit_prompt


def test_render_prompt_requires_every_placeholder():
    # a missing variable fails loudly rather than shipping a half-filled prompt.
    with pytest.raises(KeyError):
        render_prompt("select_candidates", description="d")  # no fields_line / pages_json


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
            'name=wq.doc.select(".name").attr("text"), '
            'price=wq.doc.select(".price").attr("text")).project()')

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
        name=wq.doc.select(".name").attr("text"),
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
        .extract(name=wq.doc.select(".name").attr("text")).project()
    )
    exe = _executable_query(doc_query, httpserver.url_for("/cloud"), None)
    art = QueryArtifact(
        blob=exe.to_blob(), describe=exe.describe(),
        base_urls=[httpserver.url_for("/cloud"), httpserver.url_for("/onprem")],
    )
    with WebClient() as wc:
        rows = run_query(art, wc=wc)
    assert [r["name"] for r in rows] == ["Cloud A", "Cloud B", "OnPrem X"]  # unioned


def test_model_price_includes_cache_read_and_write_costs():
    from webclient.pipelines import Usage
    from webclient.llm.client import ModelPrice, price_for

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
    from webclient.llm.client import LlmClient, ModelPrice, PRICING

    client = LlmClient(model="claude-opus-5", auth="k",
                       pricing={**PRICING, "claude-opus-5": ModelPrice.of(6.0, 30.0)})
    assert client.price().input_usd_per_mtok == 6.0  # the override wins
    # a plain client uses the default table
    assert LlmClient(model="claude-opus-5", auth="k").price().input_usd_per_mtok == 5.0


def test_settings_pass_pricing_overrides_to_the_llm_client():
    from webclient import LlmSettings, Settings
    from webclient.llm.client import ModelPrice

    s = Settings(llm=LlmSettings(model="claude-opus-5",
                                 pricing={"claude-opus-5": ModelPrice.of(7.0, 35.0)}))
    llm = s.llm_client(auth="k")
    assert llm.price().input_usd_per_mtok == 7.0
    llm.close()


def test_summary_prints_scores_flags_reference_resolve_and_a_table():
    import io
    import logging as _logging

    from webclient.policy import BrowserPolicy, ProxyPolicy, Resolve
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
    from webclient.llm.client import LlmClient

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
    from webclient.llm.client import LlmClient, LlmError

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
    from webclient.llm.client import LlmClient, LlmError

    def handler(req: "httpx.Request") -> "httpx.Response":
        return httpx.Response(529, json={"error": {"type": "overloaded", "message": "busy"}})

    client = LlmClient(model="claude-opus-5", auth="k", max_retries=2, retry_backoff=0.0,
                       transport=httpx.MockTransport(handler))
    with pytest.raises(LlmError):
        client("hi")


def test_min_interval_rate_limits(monkeypatch):
    from webclient.llm import client as llm_mod

    slept: list[float] = []
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: slept.append(s))
    client = llm_mod.LlmClient(model="claude-opus-5", auth="k", min_interval=0.5,
                               transport=httpx.MockTransport(lambda r: _ok_response()))
    client("a")
    client("b")  # the second call must wait out the interval
    assert any(0.0 < s <= 0.5 for s in slept)


def test_ask_json_survives_an_llm_error():
    from webclient.llm.client import LlmError
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

    # distinct per-row structure so the skeleton's identical-sibling merge can't shrink it --
    # forcing it past the char budget (a repetitive real listing stays tiny). The class names
    # are long and NON-utility (a bare ``p-{i}`` would read as the Tailwind padding utility and
    # be stripped, and short unique names wouldn't fill the budget after that cleanup).
    rows = "".join(
        f'<section class="product-listing-item-{i}" data-x="{i}">'
        f'<h3 class="product-name-heading-{i}">P{i}</h3>'
        f'<span class="product-price-value-{i}">v{i}</span></section>' for i in range(3000)
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
    from webclient.policy import AntiBotPolicy, BrowserPolicy, ProxyPolicy, Resolve
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
    assert "policy=" not in plain.describe() and ".resolve(" in plain.describe()


def test_output_query_is_self_contained_and_executable(httpserver):
    # the join of the LLM's extraction with the reference + resolve is deterministic and
    # produces a SELF-CONTAINED blob: from_blob(blob).collect() (no context) fetches,
    # resolves and extracts -- executable as is.
    from webclient import WebClient, from_blob, wq
    from webclient.policy import Resolve
    from webclient.pipelines.onboarding import _executable_query

    httpserver.expect_request("/p").respond_with_data(
        "<main>" + "".join(f'<div class="r"><span class="n">P{i}</span></div>' for i in range(3)) + "</main>",
        content_type="text/html",
    )
    url = httpserver.url_for("/p")
    # the model supplies ONLY the document-level extraction
    doc_q = wq.doc.select_all(".r").extract(n=wq.doc.select(".n").attr("text")).project()
    exe = _executable_query(doc_q, url, Resolve())
    assert exe.describe().startswith(f"reference('{url}').resolve()")  # reference+resolve baked in
    with WebClient() as wc:
        rows = from_blob(exe.to_blob(), wc).collect()  # no context -- self-contained
    assert rows == [{"n": "P0"}, {"n": "P1"}, {"n": "P2"}]


def test_executable_query_strips_stray_navigation_from_the_model():
    # deterministic join: even if the model prefixed a resolve, only its extraction is
    # used (one reference + one resolve, supplied by the pipeline -- not the model).
    from webclient import wq
    from webclient.policy import Resolve
    from webclient.pipelines.onboarding import _executable_query

    stray = wq.ref.resolve().select_all(".r").extract(n=wq.doc.select(".n").attr("text")).project()
    exe = _executable_query(stray, "https://x/p", Resolve())
    # exactly one resolve, then the extraction (no double resolve)
    assert exe.describe().count(".resolve(") == 1
    assert exe.describe().startswith("reference('https://x/p').resolve().select_all")


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
    assert "does NOT extract anything" in prompts[1]  # the model was told to extract something


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
    assert _parse_query(code).describe() == want
    assert _parse_query(f"here is the query:\n```python\n{code}\n```").describe() == want  # fenced + prose
    assert _parse_query("query = " + code).describe() == want  # leading assignment dropped
    blob = wq.doc.select_all(".r").extract(n=wq.doc.select(".n").attr("text")).project().to_blob()
    assert _parse_query(blob).describe() == want  # raw-blob fallback still works
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


def test_test_query_cannot_hang_on_a_slow_per_record_resolve(httpserver):
    # a pathological query (a per-record .resolve() to a hanging detail page) must NOT hang:
    # the bounded test cancels it and returns a clean failure well within the client timeout.
    import time as _time

    from webclient import wq
    from webclient.pipelines.onboarding import _test_query

    httpserver.expect_request("/list").respond_with_data(
        '<main><div class="r"><a href="/slow">x</a></div></main>', content_type="text/html",
    )

    def _hang(_req):
        _time.sleep(4)  # outlasts the 1.5s test cap
        from werkzeug.wrappers import Response
        return Response("late", content_type="text/html")

    httpserver.expect_request("/slow").respond_with_handler(_hang)

    query = wq.doc.select_all(".r").extract(
        detail=wq.doc.select("a").attr("href").resolve().select(".d").attr("text"),
    ).project()
    with WebClient() as wc:
        doc = wc.fetch(httpserver.url_for("/list"))
        t0 = _time.monotonic()
        ok, rows = _test_query(doc.select_all(".r") and query, doc, timeout=1.5)
        elapsed = _time.monotonic() - t0
    assert ok is False and rows == []  # cancelled -> a clean failed attempt
    assert elapsed < 3.5  # bounded near the 1.5s cap, not the 4s server sleep


def test_onboard_company_is_a_pipeline_with_an_interactive_confirm_gate(site):
    """The orchestrator is a Pipeline (roadmap N12): stage boundaries are PipelineEvents on
    the client's bus, and ``interactive=True`` checkpoints after the evaluation with an Ask
    the caller answers through ``result.resume``."""
    from webclient import PipelineEvent

    products_url = site.url_for("/products")
    code = ('wq.doc.select_all(".product").extract(name=wq.doc.select(".name").attr("text"), '
            'price=wq.doc.select(".price").attr("text")).project()')

    def search(query, k):
        return [SearchHit(url=site.url_for("/"), title="Acme", snippet="widgets")]

    def llm(prompt: str) -> str:
        if "frontier links" in prompt:
            for line in prompt.splitlines():
                s = line.strip()
                if s[:1].isdigit() and "/products" in s:
                    return f"[{s.split('.', 1)[0]}]"
            return "[]"
        if "crawled pages" in prompt:
            return json.dumps([{"url": products_url, "kind": "page", "tier": "must", "note": "list"}])
        if "Assess this page" in prompt:
            return json.dumps({"dataset_present": True, "is_queryable": True, "completeness": "full",
                               "has_pagination": False, "scrapability": 9, "verdict": "ok"})
        if "query code" in prompt or "write a query" in prompt:
            return f"here is the query:\n{code}"
        return "{}"

    with WebClient() as wc:
        seen = []
        wc.bus.subscribe("pipeline", seen.append)
        result = onboard_company(
            "Acme", Brief(description="the company's products", fields=["name", "price"], search="products"),
            wc=wc, llm=llm, search=search, browser=False, interactive=True,
        )
        assert result.pending is not None and result.pending.reason == f"proceed with {products_url}?"
        assert result.pending.options == ["yes", "no"] and not result.ok and result.query is None
        stages = [e.stage for e in seen if isinstance(e, PipelineEvent) and e.phase == "exit"]
        assert stages == ["search", "crawl", "select", "evaluate"]
        result = result.resume("yes")
        assert result.pending is None and result.ok, result.reason
        assert result.query is not None and result.query.row_count == 3
        exits = [e.stage for e in seen if isinstance(e, PipelineEvent) and e.phase == "exit"]
        assert exits[-2:] == ["source", "query"]
        with pytest.raises(RuntimeError):
            result.resume("again")
        # declining stops cleanly
        declined = onboard_company(
            "Acme", Brief(description="the company's products", fields=["name", "price"], search="products"),
            wc=wc, llm=llm, search=search, browser=False, interactive=True,
        ).resume("no")
        assert declined.exited and declined.reason == "declined at the confirm gate" and not declined.ok


def test_query_assessments_from_dataset_shape():
    # the completeness / correctness notes are derived from the page's dataset SHAPE
    # (doc.dataset() -- the pagination / filtered / ordered signals), not the model.
    from webclient.core.document.models import DatasetHint, Filtering, Ordering
    from webclient.pipelines.onboarding.query_assess import completeness_note, correctness_note

    plain = DatasetHint(url="http://x/list")  # one page, unfiltered, order unknown
    assert completeness_note(plain, paginated=False) == ("COMPLETENESS: one page -- the whole dataset is on a single page.", True)
    assert correctness_note(plain)[1] is True

    filtered = DatasetHint(url="http://x/list?cat=a", filtered=Filtering(active={"cat": "a"}, controls=["cat"]))
    cnote, covers = completeness_note(filtered, paginated=False)
    assert covers is False and "SUBSET" in cnote
    rnote, correct = correctness_note(filtered)
    assert correct is False and "active filter" in rnote

    ordered = DatasetHint(url="http://x/list", ordered=Ordering(key="date", direction="desc"))
    rnote2, correct2 = correctness_note(ordered)
    assert correct2 is True and "newest-first" in rnote2

    # a pager was EXPECTED but a distinct next page could not be confirmed (a JSON cursor/keyset the
    # pager can't yet walk): be HONEST it is a possible subset, not "the whole dataset on one page".
    cnote3, covers3 = completeness_note(plain, paginated=False, pager_unconfirmed=True)
    assert covers3 is False and "could NOT be confirmed" in cnote3 and "FIRST page only" in cnote3
    # confirmed-and-walked stays complete; the unconfirmed flag only fires when NOT paginated
    assert completeness_note(plain, paginated=True)[1] is True


def test_write_query_authors_a_single_value_dataset(httpserver):
    # genericity: a dataset that is ONE value (a lone datum), not a repeating list -- a
    # document-level read (.select(...).attr(...), no .select_all) is valid, not rejected as
    # "extracts nothing". (A single multi-field RECORD uses .select_all on its one container.)
    from webclient.pipelines.onboarding import write_query

    httpserver.expect_request("/co").respond_with_data(
        '<main><span class="founded">1998</span></main>', content_type="text/html",
    )

    def llm(prompt: str) -> str:
        return 'wq.doc.select(".founded").attr("text")'

    with WebClient() as wc:
        art = write_query(
            httpserver.url_for("/co"), Brief(description="the founding year", fields=["founded"]),
            wc=wc, llm=llm, browser="never", retries=0,
        )
    assert art is not None and art.tested and art.row_count == 1 and art.sample == ["1998"]


def test_a_raising_stage_surfaces_its_reason():
    # a stage that RAISES (a missing dep, a network hiccup) must not leave a bare "failed" --
    # its reason is surfaced on the result so the UI can show WHY (regression for the empty reason).
    from webclient.pipelines import Brief, onboard_company

    def bad_search(query, k):
        raise RuntimeError("web search needs the 'ddgs' package (pip install ddgs)")

    with WebClient() as wc:
        r = onboard_company(
            "Acme", Brief(description="the blog posts", fields=["title"], search="blog"),
            wc=wc, llm=lambda p: "{}", search=bad_search, browser=False,
        )
    assert not r.ok and r.reason.startswith("RuntimeError") and "ddgs" in r.reason


def test_recency_retry_is_capped_to_one(httpserver):
    # a COMPLETE-but-STALE query (fixed old dates) must not loop chasing fresher data that isn't
    # there -- pushed for recency ONCE, then the working query is kept. (The "struggling to write
    # the query" bug: a correct query was being rejected and retried 4x.)
    from webclient.pipelines import Brief
    from webclient.pipelines.onboarding import write_query

    httpserver.expect_request("/f").respond_with_data(
        '<ul><li class="i"><h3>A</h3><time datetime="2020-01-01">x</time></li>'
        '<li class="i"><h3>B</h3><time datetime="2020-01-02">y</time></li>'
        '<li class="i"><h3>C</h3><time datetime="2020-01-03">z</time></li></ul>', content_type="text/html")
    calls: list[str] = []

    def llm(prompt: str) -> str:
        calls.append(prompt)
        return ('wq.doc.select_all("li.i").extract(title=wq.doc.select("h3").attr("text"), '
                'date=wq.doc.select("time").attr("datetime")).project()')

    with WebClient() as wc:
        q = write_query(httpserver.url_for("/f"), Brief(description="news", fields=["title", "date"]),
                        wc=wc, llm=llm, browser="never", retries=4)
    assert q is not None and q.complete and q.stale  # a working query is kept, flagged stale
    assert len(calls) == 2  # authored once, pushed for recency ONCE, then kept -- not 5


def test_typographic_punctuation_in_a_query_parses(httpserver):
    # a reply with smart quotes / an em-dash must not fail parsing (it was burning a retry).
    from webclient.pipelines import Brief
    from webclient.pipelines.onboarding import write_query

    httpserver.expect_request("/p").respond_with_data('<div class="r"><span class="n">A</span></div>', content_type="text/html")

    def llm(prompt: str) -> str:  # smart quotes around the selectors
        return 'wq.doc.select_all(“div.r”).extract(n=wq.doc.select(“.n”).attr(“text”)).project()'

    with WebClient() as wc:
        q = write_query(httpserver.url_for("/p"), Brief(description="rows", fields=["n"]), wc=wc, llm=llm, browser="never", retries=0)
    assert q is not None and q.complete and q.row_count == 1
