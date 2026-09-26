"""A LIVE evaluation of the onboarding pipeline across diverse stakeholder-brief shapes.

Each case is a (brief, lab source, expected query) run END-TO-END through the whole pipeline
(search -> crawl -> select -> evaluate -> source -> query) against the in-process lab, with the
scripted SHIM model (deterministic, $0). The shim only plays the MODEL's part -- it seeds the
pipeline at the source, picks it through the crawl/select, gives a positive evaluation, and
returns the case's query code. Everything STRUCTURAL -- pagination, filtering, ordering, the XHR
API, the document kind -- comes from the REAL signals on the REAL fixtures, so this is a genuine
live evaluation of what the pipeline concludes and authors, not a mock.

The cases span the shapes a stakeholder brief takes: static records, prices ACROSS MANY PAGES (so
A/latest != B/all), a data table, a SINGLE record (genericity), a SPLIT dataset, an XHR-backed
feed, and a PDF download. Each asserts the pipeline LOCATED the source and that its A (latest) and
B (all) queries, RUN against the live source, return the expected rows."""

import json

import pytest

from webclient import WebClient
from webclient.lab import LabServer
from webclient.pipelines import Brief, SearchHit, onboard_company
from webclient.pipelines.onboarding import run_query


class Shim:
    """The scripted model + search for one eval case: seed at ``url``, pick it through the crawl
    and select, evaluate it as the dataset, and author ``code`` (1..N ``---``-separated queries)."""

    def __init__(self, url: str, code: str, *, queryable: bool = False) -> None:
        self.url, self.code, self.queryable = url, code, queryable

    def search(self, query: str, k: int) -> list:
        return [SearchHit(url=self.url, title="source", snippet="the dataset")]

    def llm(self, prompt: str) -> str:
        if "frontier links" in prompt:  # pick nothing -> the crawl fetches the seed directly
            return "[]"
        if "crawled pages" in prompt:  # the seed is the must-evaluate source
            return json.dumps([{"url": self.url, "kind": "page", "tier": "must", "note": "the dataset"}])
        if "Assess this page" in prompt:
            return json.dumps({"dataset_present": True, "is_queryable": self.queryable,
                               "completeness": "full", "scrapability": 9, "verdict": "the dataset"})
        if "query code" in prompt or "write a query" in prompt:
            return self.code
        return "{}"


# name, source path, brief fields, the query code, expected A/latest + B/all row counts, browser.
CASES = [
    dict(
        name="shop-static-records", path="/lab/shop", fields=["title", "price"],
        code='wq.doc.select_all("div.card").extract('
             'title=wq.doc.select(".title").attr("text"), price=wq.doc.select(".price").attr("text")).project()',
        latest_rows=3, all_rows=3, browser=False,
    ),
    dict(
        name="prices-across-pages", path="/lab/paginated", fields=["name"],
        code='wq.doc.select_all("article.row").extract(name=wq.doc.select(".name").attr("text")).project()',
        latest_rows=4, all_rows=12, browser=False,  # A = page one (4); B = all 3 pages (12)
    ),
    dict(
        name="a-data-table", path="/lab/table", fields=["item", "price", "stock"],
        code='wq.doc.select_all("tbody tr").extract('
             'item=wq.doc.select("td:nth-of-type(1)").attr("text"), '
             'price=wq.doc.select("td:nth-of-type(2)").attr("text"), '
             'stock=wq.doc.select("td:nth-of-type(3)").attr("text")).project()',
        latest_rows=3, all_rows=3, browser=False,
    ),
    dict(  # genericity: a SINGLE record (one container, not a repeating list)
        name="a-single-record", path="/lab/structured", fields=["name", "price"],
        code='wq.doc.select_all("main").extract('
             'name=wq.doc.select(".name").attr("text"), price=wq.doc.select(".price").attr("text")).project()',
        latest_rows=1, all_rows=1, browser=False,
    ),
    dict(  # a dataset SPLIT across differently-shaped sections (--- separated, rows concatenated)
        name="a-split-dataset", path="/lab/sections", fields=["what"],
        code='wq.doc.select_all("div.callout").extract(what=wq.doc.select(".what").attr("text")).project()'
             '\n---\n'
             'wq.doc.select_all("li.past").extract(what=wq.doc.select(".what").attr("text")).project()',
        latest_rows=4, all_rows=4, browser=False,  # 1 upcoming + 3 archived, concatenated
    ),
    dict(  # an XHR-backed feed: the records are rendered from a JSON API (needs a browser)
        name="an-xhr-feed", path="/lab/feed", fields=["title", "date"],
        code='wq.doc.select_all("li.item").extract('
             'title=wq.doc.select("h3").attr("text"), date=wq.doc.select("time").attr("datetime")).project()',
        latest_rows=3, all_rows=3, browser=True,
    ),
    dict(  # a PDF download: a binary document -- the deliverable is the FILE, not a row query
        name="a-pdf-download", path="/lab/pdf", fields=["document"], code="", browser=False, binary=True,
    ),
]


@pytest.fixture(scope="module")
def lab():
    with LabServer() as srv:
        yield srv.base


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_onboarding_eval(lab, case):
    url = f"{lab}{case['path']}"
    shim = Shim(url, case["code"])
    brief = Brief(description=f"the {case['name']} dataset", fields=case["fields"], search="data")
    with WebClient(timeout=25.0) as wc:
        result = onboard_company("Src", brief, wc=wc, llm=shim.llm, search=shim.search,
                                 browser=case["browser"])
        assert result.ok, f"{case['name']}: {result.reason}"
        assert result.evaluation is not None and result.evaluation.url == url
        assert result.query is not None
        if case.get("binary"):  # a PDF / binary: the deliverable is the file itself, not rows
            from webclient import from_blob
            assert result.query.mode == "single" and "download" in result.query.describe
            fetched = from_blob(result.query.blob, wc).collect()
            assert fetched.kind == "binary"
            return
        assert result.query_all is not None and result.query_latest is not None
        # RUN each query against the live source: A pulls the latest, B pulls the whole dataset.
        latest = run_query(result.query_latest, wc=wc)
        every = run_query(result.query_all, wc=wc)
    assert len(latest) == case["latest_rows"], f"{case['name']} A/latest: {len(latest)} != {case['latest_rows']}"
    assert len(every) == case["all_rows"], f"{case['name']} B/all: {len(every)} != {case['all_rows']}"
