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
    with WebClient() as wc, wc.trace(tmp_path / "traces" / "run1.jsonl"):
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
        summary = api.get("/traces/run1").json()
        assert summary["snapshots"] == 1 and summary["schema_version"] == 2 and summary["plan"] is False
        events = api.get("/traces/run1/events").json()
        topics = {e["topic"] for e in events}
        assert {"trace", "snapshot", "network.navigation", "error"} <= topics
        snap = next(e for e in events if e["topic"] == "snapshot")
        assert "content" not in snap  # the wire view; the event in full has it
        assert api.get(f"/traces/run1/events/{snap['n']}").json()["content"] == PAGE
        nav = next(e for e in events if e["topic"] == "network.navigation")
        assert nav["url"] == url
        err = next(e for e in events if e["topic"] == "error")
        assert err["error"]["code"] == "select.no_match"
        rr = api.get("/traces/run1/rrweb").json()  # a static run still replays: a synthesised snapshot + custom events
        assert [r["type"] for r in rr][:2] == [4, 2] and any(r["type"] == 5 for r in rr)
        assert api.get("/traces/run1/har").json()["log"]["entries"][0]["request"]["url"] == url
        assert api.get("/traces/run1/plan").status_code == 404
        assert api.get("/traces/../run1").status_code == 404
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


def test_recording_session_live_page_and_event_history(httpserver):
    """A LIVE page held by a server-side session (a plan: resolve(browser=True, keep_alive=True)),
    driven by further plans on that document, with the rrweb recorder on for the session --
    the chunks reach the UI through the bus history over HTTP (payload=true)."""
    httpserver.expect_request("/live").respond_with_data(
        "<html><head><title>Live</title></head><body><button id='b' onclick=\"document.body.insertAdjacentHTML('beforeend','<p class=added>hi</p>')\">go</button></body></html>",
        content_type="text/html")
    wc = WebClient(timeout=15.0)
    with TestClient(create_app(wc)) as api:
        sid = api.post("/sessions", json={"record": True}).json()["id"]
        assert wc._the_engine().dom_recorders == 1
        plan = {"root": "Reference", "session_id": sid, "steps": [
            {"kind": "get", "name": "resolve"},
            {"kind": "call", "name": "resolve", "args": [], "kwargs": {"browser": {"value": True}, "keep_alive": {"value": True}}}]}
        out = api.post("/execute", json={"plan": plan, "url": httpserver.url_for("/live")}).json()
        doc = out["rows"]["__doc__"]
        assert doc["live"] is True and doc["title"] == "Live"
        act = {"root": "Document", "session_id": sid, "steps": [
            {"kind": "get", "name": "click"}, {"kind": "call", "name": "click", "args": [{"value": "#b"}], "kwargs": {}},
            {"kind": "get", "name": "wait_for"}, {"kind": "call", "name": "wait_for", "args": [{"value": ".added"}], "kwargs": {}}]}
        out2 = api.post("/execute", json={"plan": act, "document_id": doc["id"]}).json()
        assert out2["rows"]["__doc__"]["id"] == doc["id"]
        chunks = api.get(f"/events?topic=rrweb&document_id={doc['id']}&payload=true").json()
        assert chunks and all(c["events"] for c in chunks)  # the DOM stream, with bodies
        types = {e["type"] for c in chunks for e in c["events"]}
        assert 2 in types and 3 in types  # a full snapshot, then the click's mutations
        assert "events" not in api.get(f"/events?topic=rrweb&document_id={doc['id']}").json()[0] or \
            api.get(f"/events?topic=rrweb&document_id={doc['id']}").json()[0]["events"] == []
        api.delete(f"/sessions/{sid}")
        assert wc._the_engine().dom_recorders == 0
    wc.close()


def test_snapshot_tool_includes_the_player_views(httpserver):
    httpserver.expect_request("/s").respond_with_data(PAGE, content_type="text/html")
    with WebClient() as wc, TestClient(create_app(wc)) as api:
        out = api.post("/tools/snapshot", json={"url": httpserver.url_for("/s"), "include": ["rrweb", "patterns", "records", "flags"]}).json()["result"]
        assert [r["type"] for r in out["rrweb"]] == [4, 2] and out["rrweb"][0]["data"]["width"] == 1280
        assert isinstance(out["records"], list) and isinstance(out["patterns"], list) and isinstance(out["flags"], list)
        assert out["document_id"].startswith("doc:")


def test_session_crawls_manual_auto_and_a_goal(httpserver):
    """Crawls held by a session over HTTP: a manual crawl fetches only what you pick (the
    frontier carries the page each edge came from -- the map's lines); an auto crawl runs in
    the background to its budget; a goal makes it a locate that stops at the matching page."""
    import time

    httpserver.expect_request("/").respond_with_data('<html><head><title>Home</title></head><body><a href="/a">A</a><a href="/b">B</a></body></html>', content_type="text/html")
    httpserver.expect_request("/a").respond_with_data('<html><head><title>Page A</title></head><body><a href="/c">C</a></body></html>', content_type="text/html")
    httpserver.expect_request("/b").respond_with_data('<html><head><title>Page B</title></head><body></body></html>', content_type="text/html")
    httpserver.expect_request("/c").respond_with_data('<html><head><title>Target C</title></head><body></body></html>', content_type="text/html")
    wc = WebClient(timeout=15.0)
    with TestClient(create_app(wc)) as api:
        sid = api.post("/sessions", json={}).json()["id"]
        root = httpserver.url_for("/")
        # manual: nothing fetched until stepped with picks
        st = api.post(f"/sessions/{sid}/crawls", json={"seeds": root, "mode": "manual", "max_pages": 10, "obey_robots": False}).json()
        cid = st["id"]
        assert st["pages"] == [] and st["running"] is False
        st = api.post(f"/crawls/{cid}/step", json={"picks": [root]}).json()
        assert [p["title"] for p in st["pages"]] == ["Home"]
        assert {e["url"] for e in st["frontier"]} == {httpserver.url_for("/a"), httpserver.url_for("/b")}
        assert all(e["parent"] == root for e in st["frontier"])  # the map's lines
        st = api.post(f"/crawls/{cid}/step", json={"picks": [httpserver.url_for("/b")]}).json()
        assert [p["title"] for p in st["pages"]] == ["Home", "Page B"]
        assert api.delete(f"/crawls/{cid}").json()["status"] == "closed"
        # auto with a goal: a locate that stops at "Target"
        st = api.post(f"/sessions/{sid}/crawls", json={"seeds": root, "mode": "auto", "max_pages": 10, "width": 2, "obey_robots": False, "goal": {"title_contains": "target"}}).json()
        cid = st["id"]
        for _ in range(100):
            st = api.get(f"/crawls/{cid}").json()
            if st["result"] is not None or st["error"]:
                break
            time.sleep(0.1)
        assert st["error"] is None and st["result"]["reason"] == "found" and st["result"]["found"] == [httpserver.url_for("/c")]
        assert [p["title"] for p in st["pages"]][-1] == "Target C"
        assert api.get(f"/sessions/{sid}/crawls").json()[0]["id"] == cid
        api.delete(f"/sessions/{sid}")
    wc.close()


def test_session_documents_static_and_live_with_reload(httpserver):
    """The documents a session holds -- what the UI lists at the top: a static capture and a
    LIVE page, their views without a refetch, a reload under the same id, and close."""
    hits = {"n": 0}

    def page(_req):
        from werkzeug.wrappers import Response
        hits["n"] += 1
        return Response(f'<html><head><title>V{hits["n"]}</title></head><body><div class="card"><h2 class="title">A</h2></div><button id="b">go</button></body></html>', content_type="text/html")

    httpserver.expect_request("/d").respond_with_handler(page)
    wc = WebClient(timeout=15.0)
    with TestClient(create_app(wc)) as api:
        sid = api.post("/sessions", json={"record": True}).json()["id"]
        url = httpserver.url_for("/d")
        st = api.post(f"/sessions/{sid}/documents", json={"url": url}).json()
        assert st["live"] is False and st["tier"] == "static" and st["title"] == "V1"
        live = api.post(f"/sessions/{sid}/documents", json={"url": url, "browser": "always"}).json()
        assert live["live"] is True and live["tier"] == "browser"
        listed = api.get(f"/sessions/{sid}/documents").json()
        assert [d["id"] for d in listed] == [st["id"], live["id"]]
        v = api.get(f"/sessions/{sid}/documents/{st['id']}/views?include=card,rrweb,records,controls,skeleton").json()
        assert v["card"]["title"] == "V1" and [r["type"] for r in v["rrweb"]] == [4, 2]
        assert v["controls"][0]["role"] == "button" and v["controls"][0]["name"] == "go"
        assert "card" in v["skeleton"] and isinstance(v["records"], list)
        re = api.post(f"/sessions/{sid}/documents/{st['id']}/reload").json()
        assert re["id"] == st["id"] and re["title"] != "V1"  # fetched again, same handle id
        assert api.get(f"/sessions/{sid}/documents/{st['id']}/views?include=card").json()["card"]["title"] == re["title"]
        re2 = api.post(f"/sessions/{sid}/documents/{live['id']}/reload").json()
        assert re2["live"] is True
        assert api.delete(f"/sessions/{sid}/documents/{live['id']}").json()["status"] == "closed"
        assert [d["id"] for d in api.get(f"/sessions/{sid}/documents").json()] == [st["id"]]
        assert api.get(f"/sessions/{sid}/documents/nope/views").status_code == 404
        api.delete(f"/sessions/{sid}")
    wc.close()
