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
