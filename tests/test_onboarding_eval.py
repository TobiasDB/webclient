"""A LIVE evaluation of the onboarding pipeline across diverse stakeholder-brief shapes.

Each case (``webclient.pipelines.examples.SPECS``) is run END-TO-END through the whole pipeline
(search -> crawl -> select -> evaluate -> source -> query) against the in-process lab, with the
scripted SHIM model (deterministic, $0). The shim only plays the MODEL's part; everything
STRUCTURAL -- pagination, filtering, the XHR API, the document kind -- comes from the REAL signals
on the REAL fixtures, so this is a genuine live evaluation, not a mock. The SAME specs drive the
service's ``GET /examples``, so the examples the UI shows are exactly what this test verifies.

The cases span the shapes a stakeholder brief takes: static records, prices ACROSS MANY PAGES (so
A/latest != B/all), a data table, a SINGLE record (genericity), a SPLIT dataset, an XHR-backed
feed, and a PDF download. Each RUNS its A (latest) and B (all) queries against the live source and
asserts the rows."""

import pytest

from webclient import WebClient, from_blob
from webclient.lab import LabServer
from webclient.pipelines.examples import SPECS, Shim, build_example
from webclient.pipelines.onboarding import run_query


@pytest.fixture(scope="module")
def lab():
    with LabServer() as srv:
        yield srv.base


@pytest.mark.parametrize("spec", SPECS, ids=[s["name"] for s in SPECS])
def test_onboarding_eval(lab, spec):
    url = f"{lab}{spec['path']}"
    with WebClient(timeout=25.0) as wc:
        result = build_example(wc, lab, spec)
        assert result.ok, f"{spec['name']}: {result.reason}"
        assert result.evaluation is not None and result.evaluation.url == url
        assert result.query is not None
        if spec.get("binary"):  # a PDF / binary: the deliverable is the file itself, not rows
            assert result.query.mode == "single" and "download" in result.query.describe
            assert from_blob(result.query.blob, wc).collect().kind == "binary"
            return
        assert result.query_all is not None and result.query_latest is not None
        # RUN each query against the live source: A pulls the latest, B pulls the whole dataset.
        latest = run_query(result.query_latest, wc=wc)
        every = run_query(result.query_all, wc=wc)
    assert len(latest) == spec["latest_rows"], f"{spec['name']} A/latest: {len(latest)} != {spec['latest_rows']}"
    assert len(every) == spec["all_rows"], f"{spec['name']} B/all: {len(every)} != {spec['all_rows']}"


def test_build_examples_produces_openable_plans(lab):
    # the wire view the UI consumes: each example carries the source + brief and the A/B query
    # PLANS (openable in Author) + BLOBS (runnable in Run), with the three assessments.
    from webclient.pipelines.examples import build_examples

    with WebClient(timeout=25.0) as wc:
        examples = build_examples(wc, lab)
    assert [e["name"] for e in examples] == [s["name"] for s in SPECS]
    for ex in examples:
        assert ex["ok"], f"{ex['name']}: {ex['reason']}"
        assert ex["source"] and ex["brief"]["fields"]
        for which in ("latest", "all"):
            art = ex[which]
            if art is None:  # only the binary download omits the row queries
                assert ex["binary"]
                continue
            assert art["blob"] and art["describe"]  # a self-contained, runnable blob + a label
            # a plain query carries its plan (openable in Author); a split query's plan is per-section
            assert art["plan"] or art["mode"] == "all"
            assert "completeness" in art and "correctness" in art  # the assessments travel with it


def test_service_examples_and_onboard_endpoints():
    # GET /examples: the worked onboardings the UI shows, built by the real pipeline (shim),
    # each with the source + A/B query plans (openable in Author, runnable in Run).
    from starlette.testclient import TestClient

    from webclient.service import create_app

    with WebClient(timeout=25.0) as wc, TestClient(create_app(wc)) as api:
        examples = api.get("/examples").json()
        assert [e["name"] for e in examples] == [s["name"] for s in SPECS]
        assert all(e["ok"] for e in examples)
        first = next(e for e in examples if not e.get("binary"))
        assert first["latest"]["blob"] and first["all"]["blob"] and first["source"]
        # POST /onboard validates its input before any model call (company + brief required).
        assert api.post("/onboard", json={}).status_code == 400
        assert api.post("/onboard", json={"company": "Acme"}).status_code == 400
