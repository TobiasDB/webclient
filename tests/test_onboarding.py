"""The onboarding pipeline end-to-end, offline: a stub search + a stub LLM over a
local two-page "company" site. Proves each stage wires to the next and that the
authored query actually extracts the dataset when run."""

import json

import pytest

from webclient import WebClient, from_blob, wq
from webclient.core.document.models import Flag
from webclient.pipelines import (
    Brief,
    Candidate,
    SearchHit,
    evaluate_candidate,
    onboard_company,
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

    # the authored query actually extracts the dataset when run against the page
    with WebClient() as wc:
        rows = from_blob(result.query.blob).collect(wc.ref(products_url))
    assert [r["name"] for r in rows] == ["Widget", "Sprocket", "Cog"]
    assert rows[0]["price"] == "$10"


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
