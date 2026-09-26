"""Traces = ONE event stream + the replay interface over it (roadmap N3, revised): record a
run -> one JSONL file -> replay offline -> identical read-only answers; the HAR and the rrweb
list are TRANSLATIONS of that stream, never stored artefacts; the plan rides in the footer."""

import json

import pytest

from webclient import RETURN, SnapshotEvent, WebClient, WebException
from webclient.replay import Replay
from webclient.replay.har import HarTransport, har_from_events, load_har
from webclient.replay.rrweb import from_rrweb, to_rrweb
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


def test_trace_is_one_stream_with_payloads_inline(site, tmp_path):
    path = tmp_path / "run.jsonl"
    wc, doc, api, titles, trace = _record(site, path)
    assert path.is_file() and not any(p for p in tmp_path.iterdir() if p != path)  # ONE file, no sidecars
    lines = [json.loads(l) for l in path.read_text().splitlines()]
    assert lines[0]["topic"] == "trace" and lines[0]["phase"] == "start" and lines[0]["detail"]["schema_version"] == 2
    assert lines[-1]["topic"] == "trace" and lines[-1]["phase"] == "end" and lines[-1]["detail"]["events"] == trace.count
    reader = read(path)
    topics = [e.topic for e in reader.events]
    assert "snapshot" in topics and "error" in topics and "network.navigation" in topics
    snaps = reader.snapshots
    assert [s.document_id for s in snaps] == [doc.name, api.name, snaps[2].document_id]
    assert snaps[0].content == PAGE.encode() and snaps[0].phase == "fetch" and snaps[0].kind == "html"
    assert snaps[1].kind == "json"
    errs = reader.of("error")
    assert {e.error.code for e in errs} == {"select.no_match", "fetch.http_status"}
    # the network events carry their url as data (the live reference is not persisted)
    assert {e.url for e in reader.of("network")} == {site("/"), site("/api"), site("/missing")}
    # the HAR is a translation of the stream: every navigation with its body
    har = reader.har()
    assert {e["request"]["url"] for e in har["log"]["entries"]} == {site("/"), site("/api"), site("/missing")}
    assert reader.summary()["snapshots"] == 3 and reader.summary()["plan"] is False


def test_trace_keeps_binary_payloads_and_the_plan(httpserver, tmp_path):
    httpserver.expect_request("/bin").respond_with_data(b"\x89PNG\r\n\x1a\n\x00\xff", content_type="image/png")
    path = tmp_path / "bin.jsonl"
    with WebClient() as wc, wc.trace(path), wc.record() as rec:  # a recording session: its plan lands in the footer
        rec.fetch(httpserver.url_for("/bin"))
        blob = rec.plan.to_blob()
    reader = read(path)
    snap = reader.snapshots[0]
    assert snap.content is not None and snap.content.startswith(b"\x89PNG") and snap.kind != "html"
    assert reader.plan_blob == blob and reader.summary()["plan"] is True
    with Replay(path) as rep:
        assert rep.plan(WebClient()).describe().startswith("reference(")


def test_replay_state_is_the_unified_cursor(site, tmp_path):
    path = tmp_path / "run.jsonl"
    wc, doc, api, titles, _ = _record(site, path)
    with Replay(path) as rep:
        full = rep.state()
        assert {d.name for d in full.documents} == {doc.name, api.name, full.documents[2].name}
        assert len(full.network) == 3 and [e.error.code for e in full.errors] == ["select.no_match", "fetch.http_status"]
        first_snap = rep.reader.snapshots[0]
        early = rep.state(first_snap.n)
        assert [d.name for d in early.documents] == [doc.name] and early.errors == []
        assert rep.at(first_snap.ts).documents[0].name == doc.name


def test_rrweb_translation_both_ways(site, tmp_path):
    path = tmp_path / "run.jsonl"
    wc, doc, api, titles, _ = _record(site, path)
    reader = read(path)
    rr = reader.rrweb(doc.name)
    types = [r["type"] for r in rr]
    assert types[:2] == [4, 2]  # a static run: Meta + a FullSnapshot synthesised from the snapshot
    assert 5 in types  # ...and the other events as custom events, tagged with their topic
    tags = {r["data"]["tag"] for r in rr if r["type"] == 5}
    assert "network.navigation" in tags and "snapshot" in tags
    node = rr[1]["data"]["node"]
    assert node["type"] == 0 and any(c.get("tagName") == "html" for c in node["childNodes"])
    # back: the rrweb list becomes our events again (the snapshot re-serialised, the custom payloads typed)
    back = from_rrweb(rr, document_id=doc.name)
    snap = next(e for e in back if isinstance(e, SnapshotEvent) and e.source == "rrweb")
    assert b'<h2 class="title">Aeropress</h2>' in snap.content
    assert any(e.topic == "network.navigation" and e.url == site("/") for e in back)
    assert to_rrweb(reader.events, custom=False)[0]["type"] == 4


def test_static_replay_answers_like_the_live_run(site, tmp_path):
    path = tmp_path / "run.jsonl"
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
        assert rep.har_path is not None and rep.har_path.exists() and rep.har_path.parent != path.parent


def test_har_replay_serves_the_static_tier_without_the_network(site, tmp_path):
    path = tmp_path / "run.jsonl"
    _record(site, path)
    with WebClient(har=str(path)) as wc:  # the trace itself: the HAR is built from its stream
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
    path = tmp_path / "browser.jsonl"
    with bwc.trace(path):
        live = bwc.ref(httpserver.url_for("/app")).resolve(browser=True).collect()
        live.click("#add")
        live.wait_for("#cart li")
        assert len(live.select_all("#cart li")) == 2
        bwc.release(live)  # drains the last captured responses before the page goes
    reader = read(path)
    phases = [s.phase for s in reader.snapshots]
    assert phases[0] == "load" and "action" in phases
    last = reader.snapshots[-1]
    assert b">a</li>" in last.content  # (nodes carry data-wc-node stamps in the raw capture)
    # every response the browser saw is a network.resource event WITH its body -> the HAR
    res = reader.of("network.resource")
    assert {e.url for e in res} >= {httpserver.url_for("/app"), httpserver.url_for("/api/items")}
    assert next(e for e in res if e.url.endswith("/api/items")).body == b'{"items":["a","b"]}'
    assert {e["request"]["url"] for e in reader.har()["log"]["entries"]} >= {httpserver.url_for("/app"), httpserver.url_for("/api/items")}
    assert reader.rrweb(live.name)[0]["type"] in (0, 1, 4) and any(r["type"] == 3 for r in reader.rrweb(live.name))
    with Replay(path) as rep:
        doc = rep.document(live.name)  # the LAST state: after the click
        assert doc is not None and [li.attr("text") for li in doc.select_all("#cart li")] == ["a", "b"]
        first = rep.document(live.name, last=False)
        assert first is not None and not first.select_all("#cart li")


def test_browser_har_replay_serves_the_page_offline(httpserver, tmp_path):
    from webclient import BrowserConfig

    httpserver.expect_request("/app").respond_with_data(APP, content_type="text/html")
    httpserver.expect_request("/api/items").respond_with_data('{"items":["a","b"]}', content_type="application/json")
    path = tmp_path / "rec.jsonl"
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


def test_trace_stores_a_repeated_body_once_and_reads_it_back(tmp_path):
    # every page of a site re-downloads the same bundles: a body is written once, later copies by hash
    from webclient.models import NetworkEvent
    from webclient.trace import Trace, read

    big = b"x" * 5000
    binary = bytes(range(256)) * 20
    path = tmp_path / "t.jsonl"
    with Trace(path) as tr:
        for i in range(3):
            tr.write(NetworkEvent(topic="network.resource", url=f"https://a/{i}", body=big, started=100.0 + i, elapsed=0.25))
            tr.write(NetworkEvent(topic="network.resource", url=f"https://b/{i}", body=binary))
    assert path.stat().st_size < 3 * len(big)  # not three copies
    got = [e for e in read(path).events if e.topic == "network.resource"]
    assert [e.body for e in got] == [big, binary] * 3
    assert got[2].started == 101.0 and got[2].elapsed == 0.25
