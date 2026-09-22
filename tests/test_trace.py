"""Traces + the static replay engine + HAR replay (roadmap N3 / Phase 1 exit criterion):
record a run -> trace dir -> replay offline -> identical read-only answers; re-execute
with the network served from the HAR."""

import json

import pytest

from webclient import RETURN, SnapshotEvent, WebClient, WebException
from webclient.replay import Replay
from webclient.replay.har import HarTransport, har_from_events, load_har
from webclient.trace import read

PAGE = """<html><head><title>Shop</title></head><body>
<nav><a href="/about">about</a></nav>
<main><h1>Featured</h1>
<div class="card"><h2 class="title">Aeropress</h2><span class="price">$39</span></div>
<div class="card"><h2 class="title">Grinder</h2><span class="price">$89</span></div>
<div class="card"><h2 class="title">Kettle</h2><span class="price">$59</span></div>
</main></body></html>"""


@pytest.fixture
def site(httpserver):
    httpserver.expect_request("/").respond_with_data(PAGE, content_type="text/html")
    httpserver.expect_request("/api").respond_with_data('{"items":[1,2]}', content_type="application/json")
    httpserver.expect_request("/missing").respond_with_data("no", status=404)
    return httpserver.url_for


def _record(site, path):
    with WebClient() as wc, wc.trace(path) as trace:
        doc = wc.fetch(site("/"))
        titles = [c.select(".title").attr("text") for c in doc.select_all(".card")]
        doc.select(".nope", error=RETURN)  # a RETURN-policy miss -> the ledger
        api = wc.fetch(site("/api"))
        wc.fetch(site("/missing"), optional=True)
        return wc, doc, api, titles, trace


def test_trace_writes_events_snapshots_and_a_static_har(site, tmp_path):
    path = tmp_path / "run.trace"
    wc, doc, api, titles, trace = _record(site, path)
    assert (path / "events.jsonl").exists() and (path / "static.har").exists()
    manifest = json.loads((path / "manifest.json").read_text())
    assert manifest["schema_version"] == 1 and manifest["finished"] and manifest["events"] == trace.count
    reader = read(path)
    topics = [e.topic for e in reader.events]
    assert "snapshot" in topics and "error" in topics and "network.navigation" in topics
    snaps = reader.snapshots
    assert [s.document_id for s in snaps] == [doc.name, api.name, snaps[2].document_id]
    assert snaps[0].content == PAGE.encode() and snaps[0].phase == "fetch" and snaps[0].kind == "html"
    assert snaps[1].kind == "json"
    # offloaded assets are re-inflated transparently
    assert (path / "snapshots").glob("*.html")
    errs = reader.of("error")
    assert {e.error.code for e in errs} == {"select.no_match", "fetch.http_status"}
    # the static HAR carries every navigation with its body
    har = load_har(path / "static.har")
    assert {e["request"]["url"] for e in har["entries"]} == {site("/"), site("/api"), site("/missing")}


def test_static_replay_answers_like_the_live_run(site, tmp_path):
    path = tmp_path / "run.trace"
    wc, live_doc, _api, live_titles, _ = _record(site, path)
    live_skeleton = live_doc.skeleton()
    live_flags = [f.name for f in live_doc.flags()]
    with Replay(path) as rep:
        docs = rep.documents()
        assert [d.kind for d in docs] == ["html", "json", "html"]
        doc = rep.document(live_doc.name)
        assert doc is not None and doc.title == "Shop" and doc.ok
        assert [c.select(".title").attr("text") for c in doc.select_all(".card")] == live_titles
        assert doc.skeleton() == live_skeleton
        assert [f.name for f in doc.flags()] == live_flags
        assert doc.markdown().startswith("# Featured") and doc.card().title == "Shop"
        assert doc.events_of("network")  # the navigation was routed onto the replayed doc
        api = rep.documents(phase="fetch")[1]
        assert api.select("items[1]").attr("value") == 2
        assert [e.error.code for e in rep.errors()] == ["select.no_match", "fetch.http_status"]
        # a projection never touches the network
        with pytest.raises(WebException) as info:
            rep.client.fetch(site("/"))
        assert info.value.error.code == "replay.offline"
        assert rep.har_path is not None and rep.har_path.exists()


def test_har_replay_serves_the_static_tier_without_the_network(site, tmp_path):
    path = tmp_path / "run.trace"
    _record(site, path)
    har = Replay(path).har_path
    assert har is not None
    with WebClient(har=str(har)) as wc:
        doc = wc.fetch(site("/"))
        assert doc.ok and doc.title == "Shop" and [c.select(".price").attr("text") for c in doc.select_all(".card")][0] == "$39"
        api = wc.fetch(site("/api"))
        assert api.kind == "json" and api.select("items[0]").attr("value") == 1
        miss = wc.fetch(site("/never-recorded"), optional=True)
        assert not miss.ok and miss.error.code == "replay.har_miss"
        assert wc.errors[-1].error.code == "replay.har_miss"


def test_har_transport_matching():
    har = {"log": {"entries": [
        {"request": {"method": "GET", "url": "http://x/a?p=1"},
         "response": {"status": 200, "headers": [{"name": "content-type", "value": "text/plain"}],
                      "content": {"text": "hi"}}},
    ]}}
    import httpx, asyncio

    async def go(strict):
        t = HarTransport(har, strict=strict)
        async with httpx.AsyncClient(transport=t) as c:
            r1 = await c.get("http://x/a?p=1")
            r2 = await c.get("http://x/a?p=2")
            return r1.status_code, r1.text, r2.status_code, t.misses

    assert asyncio.run(go(True)) == (200, "hi", 599, ["GET http://x/a?p=2"])
    assert asyncio.run(go(False))[2] == 200


def test_har_from_events_skips_bodiless_events():
    from webclient.models import NetworkEvent
    assert har_from_events([NetworkEvent(status_code=200)])["log"]["entries"] == []


def test_loop_events_are_published(httpserver):
    from webclient import LoopEvent
    from webclient.loop import BoundedLoop
    from webclient.events import EventBus

    bus = EventBus()
    seen = []
    bus.subscribe("loop", seen.append)
    loop = BoundedLoop(observe=lambda s, i, e: i, decide=lambda o: o,
                       done_result=lambda d: "ok" if d == 1 else None,
                       apply=lambda s, d: None, name="t", bus=bus)
    assert loop.run(None).done
    phases = [(e.phase, e.round) for e in seen]
    assert phases == [("round", 1), ("decision", 1), ("round", 2), ("decision", 2), ("done", 1)]
    assert all(isinstance(e, LoopEvent) and e.loop == "t" for e in seen)


@pytest.fixture(scope="module")
def bwc():
    with WebClient(timeout=10.0) as client:
        yield client


APP = """<html><head><title>App</title></head><body>
<div id="cart"></div>
<button id="add" onclick="fetch('/api/items').then(r=>r.json()).then(j=>{
  document.querySelector('#cart').innerHTML = j.items.map(i=>'<li>'+i+'</li>').join('')})">add</button>
</body></html>"""


def test_browser_trace_records_action_snapshots_and_a_har(httpserver, bwc, tmp_path):
    httpserver.expect_request("/app").respond_with_data(APP, content_type="text/html")
    httpserver.expect_request("/api/items").respond_with_data('{"items":["a","b"]}', content_type="application/json")
    path = tmp_path / "browser.trace"
    with bwc.trace(path):
        live = bwc.ref(httpserver.url_for("/app")).resolve(browser=True).collect()
        live.click("#add")
        live.wait_for("#cart li")
        assert len(live.select_all("#cart li")) == 2
        bwc.release(live)  # closes the context -> Playwright flushes the HAR
    reader = read(path)
    phases = [s.phase for s in reader.snapshots]
    assert phases[0] == "load" and "action" in phases
    last = reader.snapshots[-1]
    assert b">a</li>" in last.content  # (nodes carry data-wc-node stamps in the raw capture)
    assert reader.har_files and any(f.parent.name == "har" for f in reader.har_files)
    with Replay(path) as rep:
        doc = rep.document(live.name)  # the LAST state: after the click
        assert doc is not None and [li.attr("text") for li in doc.select_all("#cart li")] == ["a", "b"]
        first = rep.document(live.name, last=False)
        assert first is not None and not first.select_all("#cart li")


def test_browser_har_replay_serves_the_page_offline(httpserver, tmp_path):
    from webclient import BrowserConfig

    httpserver.expect_request("/app").respond_with_data(APP, content_type="text/html")
    httpserver.expect_request("/api/items").respond_with_data('{"items":["a","b"]}', content_type="application/json")
    path = tmp_path / "rec.trace"
    with WebClient(timeout=10.0) as wc, wc.trace(path):
        live = wc.ref(httpserver.url_for("/app")).resolve(browser=True).collect()
        live.click("#add")
        live.wait_for("#cart li")
        wc.release(live)
    har = Replay(path).har_path
    assert har is not None
    httpserver.clear()  # nothing is served live any more
    with WebClient(timeout=10.0, browser_config=BrowserConfig(replay_har=str(har))) as wc2:
        page = wc2.ref(httpserver.url_for("/app")).resolve(browser=True).collect()
        assert page.title == "App"
        page.click("#add")
        page.wait_for("#cart li")
        assert [li.attr("text") for li in page.select_all("#cart li")] == ["a", "b"]
        wc2.release(page)
