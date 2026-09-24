"""The API the separate UI (webclient-ui) consumes: CORS, the trace / loop endpoints, the plan
wireframe -- a trace viewer over stored traces, the live stream, and checkpoint resume."""

import json

import pytest
from fastapi.testclient import TestClient

from webclient import RETURN, WebClient
from webclient.loop import Ask
from webclient.service import create_app

PAGE = '<html><head><title>T</title></head><body><main><p class="x">hi</p></main></body></html>'


@pytest.fixture
def traced(httpserver, tmp_path):
    httpserver.expect_request("/p").respond_with_data(PAGE, content_type="text/html")
    with WebClient() as wc, wc.trace(tmp_path / "traces" / "run1"):
        doc = wc.fetch(httpserver.url_for("/p"))
        doc.select(".nope", error=RETURN)
    return tmp_path / "traces", httpserver.url_for("/p")


def test_api_allows_the_separate_ui_over_cors():
    wc = WebClient()
    with TestClient(create_app(wc, cors_origins=["http://localhost:5173"])) as api:
        r = api.options("/tools", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"})
        assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"
    wc.close()


def test_trace_endpoints_feed_the_viewer(traced):
    traces_dir, url = traced
    wc = WebClient()
    with TestClient(create_app(wc, traces_dir=traces_dir)) as api:
        listed = api.get("/traces").json()
        assert [t["id"] for t in listed] == ["run1"] and listed[0]["events"] > 0
        manifest = api.get("/traces/run1").json()
        assert manifest["snapshots"] == 1 and manifest["schema_version"] == 1
        events = api.get("/traces/run1/events").json()
        topics = {e["topic"] for e in events}
        assert {"snapshot", "network.navigation", "error"} <= topics
        snap = next(e for e in events if e["topic"] == "snapshot")
        assert "content" not in snap and snap["asset"].startswith("snapshots/")
        assert api.get(f"/traces/run1/asset/{snap['asset']}").text == PAGE
        err = next(e for e in events if e["topic"] == "error")
        assert err["error"]["code"] == "select.no_match"
        assert api.get("/traces/run1/rrweb").json() == []  # no browser -> no recording
        assert api.get("/traces/run1/asset/../manifest.json").status_code == 404
        assert api.get("/traces/nope").status_code == 404
        assert api.get("/traces/run1/events?topic=error").json()[0]["topic"] == "error"
    wc.close()


def test_plan_endpoint_renders_a_wireframe():
    from webclient import wq

    plan = wq.ref.resolve().select_all(".card").extract(t=wq.doc.select(".t").attr("text")).project()
    wc = WebClient()
    with TestClient(create_app(wc)) as api:
        out = api.post("/plan", json={"blob": plan.to_blob(), "wireframe": True}).json()
        assert out["valid"] and out["wireframe"].startswith("<!doctype html>") and "explain" in out
        assert "SELECT_ALL" in out["explain"]
    wc.close()


def test_waiting_loops_are_listed_and_resumable_over_http(httpserver):
    httpserver.expect_request("/").respond_with_data('<html><body><a href="/a">a</a><a href="/b">b</a></body></html>', content_type="text/html")
    for p in ("/a", "/b"):
        httpserver.expect_request(p).respond_with_data(f"<html><title>{p}</title></html>", content_type="text/html")
    wc = WebClient()
    rounds = {"n": 0}

    def driver(crawl):
        rounds["n"] += 1
        if rounds["n"] == 1:
            return Ask(reason="which first?", options=[e.url for e in crawl.frontier])
        return list(crawl.frontier)

    crawl = wc.crawl(httpserver.url_for("/"), driver=driver, browser=False, obey_robots=False, max_pages=5)
    crawl.run()
    assert crawl.pending is not None
    with TestClient(create_app(wc)) as api:
        waiting = api.get("/loops").json()
        assert len(waiting) == 1 and waiting[0]["kind"] == "crawl" and waiting[0]["ask"]["reason"] == "which first?"
        lid = waiting[0]["id"]
        out = api.post(f"/loops/{lid}/resume", json={"answer": waiting[0]["ask"]["options"][0]}).json()
        assert out["kind"] == "crawl" and out["pages"] >= 1 and not out["waiting"]
        assert api.get("/loops").json() == []
        assert api.post("/loops/nope/resume", json={"answer": "x"}).status_code == 404
    wc.close()


def test_catalogues_come_from_the_registries():
    """/signals and /errors are rendered by the docs and the website; they mirror the
    registries exactly (every flag, every detector, every catalogued code)."""
    from webclient.errors import CATALOG
    from webclient.signals.registry import DETECTORS, FLAGS

    with TestClient(create_app()) as client:
        sig = client.get("/signals").json()
        assert {f["name"] for f in sig} == set(FLAGS)
        assert sum(len(f["detectors"]) for f in sig) == len(DETECTORS)
        spa = next(f for f in sig if f["name"] == "spa")
        assert spa["remedy"] == "browser" and all(d["stage"] for d in spa["detectors"])
        errs = client.get("/errors").json()
        assert {e["code"] for e in errs} == set(CATALOG)
        assert all(e["remedy"] and e["hint"] for e in errs)
