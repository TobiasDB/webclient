"""Full-pipeline (search -> crawl -> select -> evaluate -> source -> query) regression tests, ONE
per fixed onboarding bug, driven by a scripted model over the LAB fixtures -- including the
red-herring-rich ``/lab/acme`` complex site and the focused scenario fixtures (``/lab/walled``,
``/lab/ranking``, ``/lab/frozen``). Each test reproduces the bug's real trigger through the WHOLE
pipeline and asserts the fix, distinctly and repeatably (deterministic scripted model, $0)."""

import json
import re

import pytest

from webclient import WebClient, from_blob
from webclient.lab import LabServer
from webclient.pipelines.onboarding import onboard_company, run_query
from webclient.pipelines.onboarding.artifacts import Brief, SearchHit


@pytest.fixture(scope="module")
def lab():
    with LabServer() as srv:
        yield srv.base


def scripted(*, code, candidates=None, evals=None, pick=None, default_eval=None, company="Probe"):
    """A scripted model for the WHOLE pipeline. ``code`` is the write_query reply -- a str, a list
    (one per attempt), or a callable ``n -> str``. ``candidates`` is the select stage's crawled-pages
    reply (dicts with ``url`` + ``tier``); ``evals`` maps a candidate-URL substring to that page's
    evaluate JSON; ``pick`` is a substring that selects frontier links to crawl (None -> crawl only
    the seed)."""
    n = {"q": 0}

    def next_code():
        i = n["q"]
        n["q"] += 1
        if callable(code):
            return code(i)
        if isinstance(code, list):
            return code[min(i, len(code) - 1)]
        return code

    def llm(prompt: str) -> str:
        if "frontier links" in prompt:  # crawl edge-pick
            if not pick:
                return "[]"
            picks = [ln.strip().split(".", 1)[0] for ln in prompt.splitlines()
                     if ln.strip()[:1].isdigit() and pick in ln]
            return "[" + ",".join(picks) + "]"
        if "crawled pages" in prompt:  # select_candidates
            return json.dumps(candidates or [])
        if "Candidate URL:" in prompt:  # evaluate_candidate
            m = re.search(r"Candidate URL:\s*(\S+)", prompt)
            url = m.group(1) if m else ""
            for sub, ev in (evals or {}).items():
                if sub in url:
                    return json.dumps(ev)
            return json.dumps(default_eval or {"dataset_present": True, "is_queryable": False,
                                               "completeness": "full", "scrapability": 8, "verdict": "the dataset"})
        if "query code" in prompt or "write a query" in prompt:  # write_query
            return next_code()
        return "{}"

    return llm


def run(wc, lab, path, brief, llm, *, company="Probe", browser=False):
    url = f"{lab}{path}"
    return onboard_company(company, brief, wc=wc, llm=llm,
                           search=lambda q, k: [SearchHit(url=url, title=company, snippet="")], browser=browser)


# --------------------------------------------------------------------------- #
# 1. a browser-only source (a static UA is 403'd) -- escalate the fetch AND bake the browser tier
# --------------------------------------------------------------------------- #

def test_pipeline_onboards_a_browser_only_source(lab):
    code = ('wq.doc.select_all("li.row").extract(name=wq.doc.select(".name").attr("text"), '
            'qty=wq.doc.select(".qty").attr("text")).project()')
    with WebClient(timeout=25.0) as wc:
        r = run(wc, lab, "/lab/walled", Brief(description="the inventory rows", fields=["name", "qty"]),
                scripted(code=code, default_eval={"dataset_present": True, "is_queryable": False,
                                                  "completeness": "full", "scrapability": 8, "verdict": "inventory"}),
                browser=True)
    assert r.ok and r.query is not None and r.query.row_count == 3       # evaluate/source escalated past the 403
    assert r.resolve is not None and r.resolve.browser is not None       # the browser tier is baked into the policy
    assert '"resolve"' in r.query.blob
    with WebClient(timeout=25.0) as wc:                                   # the blob REPRODUCES via the browser
        rows = run_query(r.query_all, wc=wc)
    assert len(rows) == 3 and rows[0]["name"] == "Alpha"


# --------------------------------------------------------------------------- #
# 2. a required field absent on a leading TOTAL row -> the whole query raises; guide to `optional`
# --------------------------------------------------------------------------- #

def test_pipeline_diagnoses_a_field_absent_on_the_total_row(lab):
    # attempt 1 reads the country with a NON-optional select("td a") -> raises on the "World" total row
    # (no link). The diagnostic must say so; attempt 2 makes it optional and succeeds.
    bad = ('wq.doc.select_all("table.rank tbody tr").extract('
           'country=wq.doc.select("td a").attr("text"), '
           'population=wq.doc.select("td:nth-of-type(3)").attr("text")).project()')
    good = bad.replace('select("td a")', 'select("td a", optional=True)')
    with WebClient(timeout=25.0) as wc:
        r = run(wc, lab, "/lab/ranking",
                Brief(description="countries by population", fields=["country", "population"]),
                scripted(code=[bad, good]))
    assert r.ok and r.query is not None and r.query.complete
    assert any("absent on some rows" in a for a in r.query.attempts)     # the targeted diagnostic fired
    with WebClient(timeout=25.0) as wc:
        rows = run_query(r.query_all, wc=wc)
    assert any(row.get("country") == "China" for row in rows)            # real data extracted


# --------------------------------------------------------------------------- #
# 3. a ?page= param the server IGNORES -> the shipped query must not emit duplicate rows
# --------------------------------------------------------------------------- #

def test_pipeline_ignored_pager_does_not_duplicate_rows(lab):
    code = 'wq.doc.select_all("article.item").extract(name=wq.doc.select(".name").attr("text")).project()'
    with WebClient(timeout=25.0) as wc:
        r = run(wc, lab, "/lab/frozen", Brief(description="the listing rows", fields=["name"]),
                scripted(code=code, default_eval={"dataset_present": True, "is_queryable": False,
                                                  "has_pagination": True, "completeness": "full",
                                                  "scrapability": 8, "verdict": "a paginated listing"}))
        assert r.ok and r.query is not None
        rows = run_query(r.query_all, wc=wc)
    names = [row["name"] for row in rows]
    assert names == ["Row A", "Row B", "Row C"]                          # 3 unique rows, NOT 12 duplicates


# --------------------------------------------------------------------------- #
# 3b. a ?page_num= pager whose '»' Next link LOOPS to page one -> fall back to the param pager
# --------------------------------------------------------------------------- #

def test_pipeline_falls_back_from_a_looping_next_pager(lab):
    code = 'wq.doc.select_all("article.row").extract(name=wq.doc.select(".name").attr("text")).project()'
    with WebClient(timeout=25.0) as wc:
        r = run(wc, lab, "/lab/looppager", Brief(description="the records", fields=["name"]),
                scripted(code=code, default_eval={"dataset_present": True, "is_queryable": False,
                                                  "has_pagination": True, "completeness": "full",
                                                  "scrapability": 8, "verdict": "a paginated listing"}))
        assert r.ok and r.query is not None
        rows = run_query(r.query_all, wc=wc)
    names = [row["name"] for row in rows]
    assert len(names) == 12 and len(set(names)) == 12          # all 3 pages walked via ?page_num=, no loop
    assert names[0] == "Item 1" and names[-1] == "Item 12"


# --------------------------------------------------------------------------- #
# 3c. OVERLAPPING pages + a sticky sponsored row -> the shipped query dedups the union
# --------------------------------------------------------------------------- #

def test_pipeline_dedups_overlapping_pages_and_sticky_rows(lab):
    code = 'wq.doc.select_all("li.item").extract(name=wq.doc.select(".name").attr("text")).project()'
    with WebClient(timeout=25.0) as wc:
        r = run(wc, lab, "/lab/overlap", Brief(description="the feed items", fields=["name"]),
                scripted(code=code, default_eval={"dataset_present": True, "is_queryable": False,
                                                  "has_pagination": True, "completeness": "full",
                                                  "scrapability": 8, "verdict": "a paginated feed"}))
        assert r.ok and r.query is not None and "distinct=True" in r.query.describe  # dedup baked into the blob
        names = [row["name"] for row in run_query(r.query_all, wc=wc)]
    assert names.count("Sponsored") == 1                       # the sticky row (on every page) appears once
    assert len(names) == len(set(names)) == 9                  # 8 items + 1 sponsored, no cross-page duplicates
    assert set(names) == {"Sponsored", *(f"Item {i}" for i in range(1, 9))}


# --------------------------------------------------------------------------- #
# 4. a listing that links to per-item queryable JSON endpoints -> pick the LISTING, not a drill-down
# --------------------------------------------------------------------------- #

def test_pipeline_prefers_the_listing_over_a_queryable_drilldown(lab):
    listing = f"{lab}/lab/acme/products"
    api1 = f"{lab}/lab/acme/api/products/1"
    code = ('wq.doc.select_all("section.catalogue article.product").extract(name=wq.doc.select(".name").attr("text"), '
            'price=wq.doc.select(".price").attr("text")).project()')
    # the model tiers the listing MUST and the per-record endpoint SHOULD; the endpoint evaluates as
    # queryable (a single JSON record), the listing as a scrapable page. The listing must WIN.
    candidates = [{"url": listing, "kind": "page", "tier": "must", "note": "the catalogue"},
                  {"url": api1, "kind": "page", "tier": "should", "note": "an item endpoint"}]
    evals = {"/api/products/1": {"dataset_present": True, "is_queryable": True, "completeness": "partial",
                                 "scrapability": 9, "verdict": "a JSON item endpoint"},
             "/lab/acme/products": {"dataset_present": True, "is_queryable": False, "completeness": "full",
                                    "has_pagination": True, "scrapability": 7, "verdict": "the product listing"}}
    with WebClient(timeout=25.0) as wc:
        r = run(wc, lab, "/lab/acme/products",
                Brief(description="the products with name and price", fields=["name", "price"]),
                scripted(code=code, candidates=candidates, evals=evals))
    assert r.ok and r.evaluation is not None
    assert r.evaluation.url == listing                                   # the LISTING, not /api/products/1
    assert "/api/products/" not in r.reference


# --------------------------------------------------------------------------- #
# 4b. the visible DOM is a TEASER; a JSON-LD island holds the whole dataset -> extract from the island
# --------------------------------------------------------------------------- #

def test_pipeline_prefers_a_richer_json_island_over_a_dom_teaser(lab):
    # the DOM shows 3 teaser cards but a <script type="application/ld+json"> ItemList holds 12. A query
    # over the visible cards extracts 3 rows and LOOKS complete; it must be rejected as a subset and the
    # retry hint must point at the island, where the whole dataset lives.
    dom = ('wq.doc.select_all("article.card").extract(name=wq.doc.select(".name").attr("text"), '
           'price=wq.doc.select(".price").attr("text")).project()')                    # the teaser subset
    island = ('wq.doc.select("script[type=\'application/ld+json\']").as_json().select_all("itemListElement")'
              '.extract(name=wq.doc.attr("name"), price=wq.doc.select("offers").attr("price")).project()')  # the whole set
    with WebClient(timeout=25.0) as wc:
        r = run(wc, lab, "/lab/twoface",
                Brief(description="every product with its name and price", fields=["name", "price"]),
                scripted(code=[dom, island]))
        assert r.ok and r.query is not None and r.query.complete
        assert any("JSON island" in a or "island holds" in a for a in r.query.attempts)  # the subset was caught
        rows = run_query(r.query_all, wc=wc)
    assert len(rows) == 12 and rows[-1]["name"] == "Beans"                              # the whole dataset


# --------------------------------------------------------------------------- #
# 5. a field that resolves to a JSON detail page and grabs the whole object -> rejected, drill in
# --------------------------------------------------------------------------- #

def test_pipeline_rejects_a_container_valued_field(lab):
    grab = ('wq.doc.select_all("section.catalogue article.product").extract(name=wq.doc.select(".name").attr("text"), '
            'stock=wq.doc.select("a.data").attr("href").resolve().attr("stock")).project()')          # the whole {count} object
    drill = grab.replace('.resolve().attr("stock")', '.resolve().select("stock.count").attr("text")')  # the leaf
    with WebClient(timeout=25.0) as wc:
        r = run(wc, lab, "/lab/acme/products",
                Brief(description="each product's name and stock count (on its data endpoint)",
                      fields=["name", "stock"]),
                scripted(code=[grab, drill],
                         default_eval={"dataset_present": True, "is_queryable": False, "completeness": "full",
                                       "scrapability": 8, "verdict": "products with a data endpoint"}))
    assert r.ok and r.query is not None and r.query.complete
    assert any("whole JSON object" in a or "whole object" in a for a in r.query.attempts)  # the blob diagnostic fired
    assert str(r.query.sample[0]["stock"]).isdigit()                     # a real leaf value, not '{"count": ...}'


# --------------------------------------------------------------------------- #
# 6. a required field genuinely ABSENT from the source -> fail FAST with a precise reason
# --------------------------------------------------------------------------- #

def test_pipeline_absent_field_fails_fast_with_a_precise_reason(lab):
    # the products listing has name + price, but NO rating anywhere -> the model keeps a partial; the
    # run stops EARLY (not all 5 retries) and the reason names the absent field.
    code = ('wq.doc.select_all("section.catalogue article.product").extract(name=wq.doc.select(".name").attr("text"), '
            'price=wq.doc.select(".price").attr("text"), '
            'rating=wq.doc.select(".rating", optional=True).attr("text")).project()')
    with WebClient(timeout=25.0) as wc:
        r = run(wc, lab, "/lab/acme/products",
                Brief(description="the products with name, price and star rating", fields=["name", "price", "rating"]),
                scripted(code=code))
    assert not r.ok and "rating" in r.reason and "not present on the source" in r.reason
    assert r.query is not None and r.query.absent == ["rating"] and len(r.query.attempts) < 5  # stopped early


# --------------------------------------------------------------------------- #
# 7. a filtered listing (drop sold-out rows) -> the answer excludes the sold-out rows
# --------------------------------------------------------------------------- #

def test_pipeline_filters_sold_out_rows(lab):
    code = ('wq.doc.select_all("li.product").filter(~wq.doc.select("span.sold-out", optional=True).is_ok())'
            '.extract(name=wq.doc.select(".name").attr("text")).project()')
    with WebClient(timeout=25.0) as wc:
        r = run(wc, lab, "/lab/store", Brief(description="the in-stock products", fields=["name"]),
                scripted(code=code))
        assert r.ok and r.query is not None
        names = {row["name"] for row in run_query(r.query_all, wc=wc)}
    assert "Colombia Huila" not in names and "Sumatra Mandheling" not in names  # the sold-out rows are gone
    assert names == {"Ethiopia Yirgacheffe", "Kenya AA", "Guatemala Antigua"}


# --------------------------------------------------------------------------- #
# 8. a total model OUTAGE (every LLM call errors) -> report RETRY, not "no usable source"
# --------------------------------------------------------------------------- #

def test_pipeline_reports_a_model_outage_as_retry(lab):
    from webclient.llm.client import LlmError

    def throttled(prompt: str) -> str:
        raise LlmError(1, "rate limited")

    with WebClient(timeout=25.0) as wc:
        r = run(wc, lab, "/lab/acme/products",
                Brief(description="the products", fields=["name"]), throttled)
    assert not r.ok and "unavailable" in r.reason and "no usable source" not in r.reason
