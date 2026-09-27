"""onboarding EXAMPLES -- one worked onboarding per dataset SHAPE, built against the lab with the
scripted shim (deterministic, $0). Each example is the pipeline's REAL output -- the source, the
three assessments (timeliness / completeness / correctness) and the A/latest + B/all query plans --
ready to open in the Author or run in the Run workspace. Shared by the live eval test
(:mod:`tests.test_onboarding_eval`) and the service's ``GET /examples``, so the examples the UI
shows are exactly what the pipeline produces, verified in CI."""

from __future__ import annotations

import json
from typing import Any

from ..interface import WebClient
from .onboarding import Brief, OnboardingResult, QueryArtifact, SearchHit, onboard_company


class Shim:
    """The scripted model + search for one example: seed at ``url``, pick it through the crawl and
    select, evaluate it as the dataset, and author ``code`` (1..N ``---``-separated queries). The
    STRUCTURE (pagination / filters / the XHR API / the kind) comes from the real signals -- the
    shim only plays the model's part, so an example is a genuine pipeline run, not a canned blob."""

    def __init__(self, url: str, code: str, *, queryable: bool = False) -> None:
        self.url, self.code, self.queryable = url, code, queryable

    def search(self, query: str, k: int) -> "list[SearchHit]":
        return [SearchHit(url=self.url, title="source", snippet="the dataset")]

    def llm(self, prompt: str) -> str:
        if "frontier links" in prompt:  # pick nothing -> the crawl fetches the seed directly
            return "[]"
        if "crawled pages" in prompt:
            return json.dumps([{"url": self.url, "kind": "page", "tier": "must", "note": "the dataset"}])
        if "Assess this page" in prompt:
            return json.dumps({"dataset_present": True, "is_queryable": self.queryable,
                               "completeness": "full", "scrapability": 9, "verdict": "the dataset"})
        if "query code" in prompt or "write a query" in prompt:
            return self.code
        return "{}"


#: one example per dataset shape a stakeholder brief takes. ``path`` is on the lab; ``code`` is the
#: query the model authors; ``latest_rows`` / ``all_rows`` are what A and B return when RUN (None
#: for a binary download, whose deliverable is the file). ``browser`` when the page needs rendering.
SPECS: "list[dict[str, Any]]" = [
    dict(name="shop-static-records", title="A static shop listing", path="/lab/shop",
         description="the featured products with their prices", fields=["title", "price"],
         code='wq.doc.select_all("div.card").extract('
              'title=wq.doc.select(".title").attr("text"), price=wq.doc.select(".price").attr("text")).project()',
         latest_rows=3, all_rows=3, browser=False),
    dict(name="prices-across-pages", title="Prices across many pages", path="/lab/paginated",
         description="every row of the dataset, across all its pages", fields=["name"],
         code='wq.doc.select_all("article.row").extract(name=wq.doc.select(".name").attr("text")).project()',
         latest_rows=4, all_rows=12, browser=False),  # A = page one (4); B = all 3 pages (12)
    dict(name="a-data-table", title="A data table", path="/lab/table",
         description="the price table's rows", fields=["item", "price", "stock"],
         code='wq.doc.select_all("tbody tr").extract('
              'item=wq.doc.select("td:nth-of-type(1)").attr("text"), '
              'price=wq.doc.select("td:nth-of-type(2)").attr("text"), '
              'stock=wq.doc.select("td:nth-of-type(3)").attr("text")).project()',
         latest_rows=3, all_rows=3, browser=False),
    dict(name="a-single-record", title="A single record", path="/lab/structured",
         description="the one product on the page", fields=["name", "price"],
         code='wq.doc.select_all("main").extract('
              'name=wq.doc.select(".name").attr("text"), price=wq.doc.select(".price").attr("text")).project()',
         latest_rows=1, all_rows=1, browser=False),
    dict(name="a-split-dataset", title="A split dataset", path="/lab/sections",
         description="the events, upcoming and archived", fields=["what"],
         code='wq.doc.select_all("div.callout").extract(what=wq.doc.select(".what").attr("text")).project()'
              '\n---\n'
              'wq.doc.select_all("li.past").extract(what=wq.doc.select(".what").attr("text")).project()',
         latest_rows=4, all_rows=4, browser=False),
    dict(name="an-xhr-feed", title="An XHR-backed feed", path="/lab/feed",
         description="the newsroom items (rendered from a JSON API)", fields=["title", "date"],
         code='wq.doc.select_all("li.item").extract('
              'title=wq.doc.select("h3").attr("text"), date=wq.doc.select("time").attr("datetime")).project()',
         latest_rows=3, all_rows=3, browser=True),
    dict(name="a-pdf-download", title="A PDF download", path="/lab/pdf",
         description="the PDF document", fields=["document"], code="",
         latest_rows=None, all_rows=None, browser=False, binary=True),
    dict(name="a-list-valued-field", title="A record with a list-valued field", path="/lab/quotes",
         description="each quote with its text, its author, and the full list of its tags",
         fields=["text", "author", "tags"],
         # tags is MANY values per record -- a nested select_all inside extract yields a list column.
         code='wq.doc.select_all("div.quote").extract('
              'text=wq.doc.select("span.text").attr("text"), '
              'author=wq.doc.select("small.author").attr("text"), '
              'tags=wq.doc.select_all("a.tag").attr("text")).project()',
         latest_rows=6, all_rows=6, browser=False),
    dict(name="value-in-a-class-token", title="A value in a class token", path="/lab/catalog",
         description="each book with its full title, its star rating, and its price",
         fields=["title", "rating", "price"],
         # the rating is the word in class="star-rating Three" (regex a token out of the class attr);
         # the full title is in the anchor's title attr (the visible text is truncated).
         code='wq.doc.select_all("article.product_pod").extract('
              'title=wq.doc.select("h3 a").attr("title"), '
              'rating=wq.doc.select("p.star-rating").attr("class", "star-rating ([A-Za-z]+)", group=1), '
              'price=wq.doc.select("p.price_color").attr("text")).project()',
         latest_rows=6, all_rows=6, browser=False),
    dict(name="filter-sold-out", title="A filtered listing (drop sold-out rows)", path="/lab/store",
         description="the IN-STOCK products only (exclude anything sold out), with name and price",
         fields=["name", "price"],
         # a row-level .filter() drops the sold-out rows -- the answer's correctness is the filter.
         code='wq.doc.select_all("li.product").filter(~wq.doc.select("span.sold-out", optional=True).is_ok())'
              '.extract(name=wq.doc.select(".name").attr("text"), '
              'price=wq.doc.select(".price").attr("text")).project()',
         latest_rows=3, all_rows=3, browser=False),  # 3 in-stock; the 2 sold-out rows are filtered out
    dict(name="paginate-and-resolve", title="Pagination + a per-record detail resolve", path="/lab/deep",
         description="every item across all pages, each with its name and its SKU (on the item's own page)",
         fields=["name", "sku"],
         # the deepest shape: the pipeline walks the pages AND each record resolves its link for the SKU.
         code='wq.doc.select_all("article.item").extract('
              'name=wq.doc.select(".name").attr("text"), '
              'sku=wq.doc.select("a.more").attr("href").resolve().select(".sku").attr("text")).project()',
         latest_rows=4, all_rows=12, browser=False),  # A = page one (4); B = all 3 pages resolved (12)
    dict(name="sibling-row-records", title="Records split across sibling rows", path="/lab/news",
         description="the ranked stories with each story's title, points, author and age",
         fields=["title", "points", "user", "age"],
         # rebuilt from the live Hacker News front page: the record is the title row (tr.athing) but
         # points/user/age live in the NEXT sibling row -- reached with a ":scope + tr.subtext" hop.
         code='wq.doc.select_all("tr.athing").extract('
              'title=wq.doc.select(".titleline a").attr("text"), '
              'points=wq.doc.select(":scope + tr.subtext .score").attr("text"), '
              'user=wq.doc.select(":scope + tr.subtext a.hnuser").attr("text"), '
              'age=wq.doc.select(":scope + tr.subtext .age").attr("text")).project()',
         latest_rows=6, all_rows=6, browser=False),
    dict(name="json-cursor-pagination", title="A keyset (cursor) JSON API", path="/lab/cursor",
         description="every record the JSON API returns, across all its cursor pages", fields=["id", "name"],
         # a native JSON keyset API: the pipeline DETECTS the cursor (pageInfo.endCursor) and, without
         # being told which request param it rides in, confirms ?after= by walking to a distinct page.
         code='wq.doc.select_all("items").extract('
              'id=wq.doc.attr("id"), name=wq.doc.attr("name")).project()',
         latest_rows=4, all_rows=10, browser=False),  # A = page one (4); B = all keyset pages (10)
    dict(name="a-merged-table", title="A table with merged (rowspan) cells", path="/lab/merged",
         description="each item with its category and price (the category is a merged cell spanning rows)",
         fields=["category", "item", "price"],
         # .table() expands the rowspan so every item row carries its category (a per-<tr> query can't).
         code='wq.doc.table("table.catalogue").extract('
              'category=wq.doc.attr("Category"), item=wq.doc.attr("Item"), price=wq.doc.attr("Price")).project()',
         latest_rows=5, all_rows=5, browser=False),
    dict(name="a-transposed-table", title="A transposed feature matrix (records are columns)", path="/lab/pivot",
         description="each plan with its price, users and storage (the plans run ACROSS as columns)",
         fields=["plan", "price", "users", "storage"],
         # a feature-comparison matrix: .table(transpose=True) reads each COLUMN as a record.
         code='wq.doc.table("table.compare", transpose=True).extract('
              'plan=wq.doc.attr("Plan"), price=wq.doc.attr("Price"), '
              'users=wq.doc.attr("Users"), storage=wq.doc.attr("Storage")).project()',
         latest_rows=3, all_rows=3, browser=False),
    dict(name="json-island-vs-teaser", title="A JSON island richer than the DOM", path="/lab/twoface",
         description="every product -- the DOM shows only a few teasers, the whole list is in a JSON-LD island",
         fields=["name", "price"],
         # the DOM renders 3 teaser cards but the ld+json ItemList carries all 12: select the script,
         # .as_json() into it, then select_all the list. A DOM query would ship a 3-row SUBSET as 'complete'.
         code='wq.doc.select(\'script[type="application/ld+json"]\').as_json().select_all("itemListElement")'
              '.extract(name=wq.doc.attr("name"), price=wq.doc.select("offers").attr("price")).project()',
         latest_rows=12, all_rows=12, browser=False),
    dict(name="cross-page-dedup", title="Overlapping pages + a sticky row (dedup)", path="/lab/overlap",
         description="every DISTINCT feed record across the pages (the page windows overlap and a sponsored row repeats)",
         fields=["name"],
         # the pages overlap and a 'Sponsored' row rides on every page; the pipeline bakes project(distinct=True)
         # on the paginated union so each record appears once (page-level dedup can't -- the pages ARE distinct).
         code='wq.doc.select_all("li.item").extract(name=wq.doc.select(".name").attr("text")).project()',
         latest_rows=5, all_rows=9, browser=False),  # A = page one (sponsored + 4); B = 8 items + 1 sponsored
]


def _artifact_view(qa: "QueryArtifact | None") -> "dict[str, Any] | None":
    """The wire view of a query artifact for the UI: enough to SHOW it and to open it in the Author
    (``plan``) or run it in Run (``blob``), with its assessments."""
    if qa is None:
        return None
    return {
        "mode": qa.mode, "describe": qa.describe, "blob": qa.blob, "plan": qa.plan,
        "row_count": qa.row_count, "sample": list(qa.sample[:5]),
        "timeliness": qa.timeliness, "completeness": qa.completeness, "correctness": qa.correctness,
        "covers_all": qa.covers_all, "correct": qa.correct,
    }


def result_view(result: "OnboardingResult", spec: "dict[str, Any]") -> "dict[str, Any]":
    """A finished onboarding as the UI consumes it: the source + brief, and the A/latest and B/all
    query plans (each openable in the Author, runnable in Run) with their three assessments."""
    return {
        "name": spec["name"], "title": spec.get("title", spec["name"]),
        "description": spec["description"], "source": result.evaluation.url if result.evaluation else "",
        "ok": result.ok, "reason": result.reason, "binary": bool(spec.get("binary")),
        "brief": {"description": spec["description"], "fields": list(spec["fields"])},
        "resolve": (result.resolve.model_dump(mode="json") if result.resolve is not None else {}),
        "seeds": list(result.seeds), "crawl_pages": list(result.crawl_pages), "candidates": list(result.candidates),
        "steps": list(result.steps),
        "evaluation": (result.evaluation.model_dump(mode="json") if result.evaluation is not None else None),
        "latest": _artifact_view(result.query_latest),
        "all": _artifact_view(result.query_all),
    }


def build_example(wc: WebClient, base: str, spec: "dict[str, Any]") -> "OnboardingResult":
    """Run the whole pipeline for one example against the lab at ``base`` with the shim."""
    url = f"{base}{spec['path']}"
    shim = Shim(url, spec["code"])
    brief = Brief(description=spec["description"], fields=list(spec["fields"]), search="data")
    return onboard_company("Example", brief, wc=wc, llm=shim.llm, search=shim.search,
                           browser=spec["browser"])


def build_examples(wc: WebClient, base: str) -> "list[dict[str, Any]]":
    """Every example as the UI consumes it -- run each spec through the pipeline against the lab at
    ``base`` (the caller supplies a running lab). Deterministic and free (the shim)."""
    return [result_view(build_example(wc, base, s), s) for s in SPECS]


__all__ = ["Shim", "SPECS", "build_example", "build_examples", "result_view"]
