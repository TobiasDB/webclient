"""``GET /ops``: the op catalogue a UI builds an object's menu from -- generated from the cores'
backing tables (the same source as the typed surface), never hand-listed."""

from webclient.service import op_catalogue


def test_op_catalogue_lists_document_ops_with_params():
    cat = op_catalogue()
    doc = {o["name"]: o for o in cat["Document"]}
    assert doc["select"]["params"][0] == {"name": "selector", "required": True, "kind": "positional", "default": None, "type": "str"}
    assert doc["write"]["io"] and doc["select_all"]["collection"]
    assert doc["attr"]["params"][0]["name"] == "name"
    assert doc["title"]["kind"] == "prop"
    # the hand-written chain ops ride along, with paginate's real signature
    assert doc["extract"]["bound"] and {p["name"] for p in doc["paginate"]["params"]} >= {"by", "max_pages", "next", "records"}
    assert {o["name"] for o in cat["Reference"]} >= {"resolve", "with_params"}
    ref = {o["name"]: o for o in cat["Reference"]}
    assert ref["resolve"]["returns"] == "Document" and ref["with_params"]["returns"] == "Reference"
    # the builder types its nodes from these
    assert doc["select"]["returns"] == "Document" and doc["select_all"]["returns"] == "Collection"
    assert doc["attr"]["returns"] == "Value|Reference" and doc["click"]["returns"] == "Document"
    coll = {o["name"]: o for o in cat["Collection"]}
    assert coll["attr"]["lifted"] and coll["attr"]["returns"] == "Collection"
    assert {"extract", "filter", "limit", "project", "merge"} <= set(coll)
    assert "click" not in coll  # IO ops are not lifted


def test_ops_endpoint():
    from fastapi.testclient import TestClient

    from webclient.service import create_app

    with TestClient(create_app()) as client:
        r = client.get("/ops")
        assert r.status_code == 200
        names = {o["name"] for o in r.json()["Document"]}
        assert {"select", "select_all", "attr", "click", "paginate", "markdown"} <= names


def test_open_document_reuses_a_held_capture():
    from fastapi.testclient import TestClient
    from pytest_httpserver import HTTPServer

    from webclient.service import create_app

    with HTTPServer() as srv:
        srv.expect_request("/p").respond_with_data("<html><body><h1>p</h1></body></html>", content_type="text/html")
        with TestClient(create_app()) as client:
            sid = client.post("/sessions", json={}).json()["id"]
            a = client.post(f"/sessions/{sid}/documents", json={"url": srv.url_for("/p")}).json()
            b = client.post(f"/sessions/{sid}/documents", json={"url": srv.url_for("/p")}).json()
            c = client.post(f"/sessions/{sid}/documents", json={"url": srv.url_for("/p"), "reuse": False}).json()
            assert b["id"] == a["id"] and b.get("reused") is True
            assert c["id"] != a["id"]
            assert len(srv.log) == 2  # two fetches, not three


def test_execute_an_op_the_object_lacks_is_a_422_not_a_500():
    from fastapi.testclient import TestClient
    from pytest_httpserver import HTTPServer

    from webclient.service import create_app

    with HTTPServer() as srv:
        srv.expect_request("/").respond_with_data('<html><body><a href="/x">x</a></body></html>', content_type="text/html")
        plan = {"root": "Reference", "steps": [
            {"kind": "get", "name": "resolve"}, {"kind": "call", "name": "resolve", "args": [], "kwargs": {}},
            {"kind": "get", "name": "extract"}, {"kind": "call", "name": "extract", "args": [], "kwargs": {"x": {"plan": {"root": "Document", "steps": [
                {"kind": "get", "name": "select"}, {"kind": "call", "name": "select", "args": [{"value": "a"}], "kwargs": {}},
                {"kind": "get", "name": "attr"}, {"kind": "call", "name": "attr", "args": [{"value": "href"}], "kwargs": {}},
                {"kind": "get", "name": "extract"}, {"kind": "call", "name": "extract", "args": [], "kwargs": {}}]}}}},
            {"kind": "get", "name": "project"}, {"kind": "call", "name": "project", "args": [], "kwargs": {}}]}
        with TestClient(create_app()) as client:
            r = client.post("/execute", json={"plan": plan, "url": srv.url_for("/")})
            assert r.status_code == 422, r.text
            assert r.json()["error"]["code"] == "op.unsupported"


def test_execute_can_save_the_run_as_a_trace(tmp_path):
    from fastapi.testclient import TestClient
    from pytest_httpserver import HTTPServer

    from webclient.service import create_app

    with HTTPServer() as srv:
        srv.expect_request("/").respond_with_data("<html><body><h1>Hi</h1></body></html>", content_type="text/html")
        plan = {"root": "Reference", "steps": [
            {"kind": "get", "name": "resolve"}, {"kind": "call", "name": "resolve", "args": [], "kwargs": {}},
            {"kind": "get", "name": "extract"}, {"kind": "call", "name": "extract", "args": [], "kwargs": {"h": {"plan": {"root": "Document", "steps": [
                {"kind": "get", "name": "select"}, {"kind": "call", "name": "select", "args": [{"value": "h1"}], "kwargs": {}},
                {"kind": "get", "name": "attr"}, {"kind": "call", "name": "attr", "args": [{"value": "text"}], "kwargs": {}}]}}}},
            {"kind": "get", "name": "project"}, {"kind": "call", "name": "project", "args": [], "kwargs": {}}]}
        with TestClient(create_app(traces_dir=tmp_path)) as client:
            r = client.post("/execute", json={"plan": plan, "url": srv.url_for("/"), "trace": "my run / 1"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["rows"] == {"h": "Hi"} and body["trace"] == "my-run-1"
            assert (tmp_path / "my-run-1.jsonl").exists()
            listed = {t["id"]: t for t in client.get("/traces").json()}
            assert "my-run-1" in listed and listed["my-run-1"]["events"] > 0
            assert client.get("/traces/my-run-1/plan").status_code == 200  # the plan rides in the footer


def test_runs_stream_rows_and_events_and_record_a_trace(tmp_path):
    import time as _t

    from fastapi.testclient import TestClient
    from pytest_httpserver import HTTPServer

    from webclient.service import create_app

    html = "<html><body>" + "".join(f'<li class="r"><b>{i}</b></li>' for i in range(5)) + "</body></html>"
    with HTTPServer() as srv:
        srv.expect_request("/").respond_with_data(html, content_type="text/html")
        plan = {"root": "Reference", "steps": [
            {"kind": "get", "name": "resolve"}, {"kind": "call", "name": "resolve", "args": [], "kwargs": {}},
            {"kind": "get", "name": "select_all"}, {"kind": "call", "name": "select_all", "args": [{"value": "li.r"}], "kwargs": {}},
            {"kind": "get", "name": "extract"}, {"kind": "call", "name": "extract", "args": [], "kwargs": {"n": {"plan": {"root": "Document", "steps": [
                {"kind": "get", "name": "select"}, {"kind": "call", "name": "select", "args": [{"value": "b"}], "kwargs": {}},
                {"kind": "get", "name": "attr"}, {"kind": "call", "name": "attr", "args": [{"value": "text"}], "kwargs": {}}]}}}},
            {"kind": "get", "name": "project"}, {"kind": "call", "name": "project", "args": [], "kwargs": {}}]}
        with TestClient(create_app(traces_dir=tmp_path)) as client:
            r = client.post("/runs", json={"plan": plan, "url": srv.url_for("/"), "name": "t"})
            assert r.status_code == 200, r.text
            rid = r.json()["id"]
            for _ in range(100):
                got = client.get(f"/runs/{rid}").json()
                if got["status"] != "running":
                    break
                _t.sleep(0.05)
            assert got["status"] == "done", got
            assert [x["row"] for x in got["rows"]] == [{"n": str(i)} for i in range(5)]
            assert all(isinstance(x["at"], int) for x in got["rows"])
            steps = [e for e in got["events"] if e.get("topic") == "plan" and e.get("phase") == "step"]
            assert any(e.get("detail", {}).get("op") == "select_all" for e in steps)
            assert (tmp_path / f"{rid}.jsonl").exists()
            later = client.get(f"/runs/{rid}", params={"rows": 3, "events": got["n_events"]}).json()
            assert len(later["rows"]) == 2 and later["events"] == []
            assert client.get("/runs").json()[0]["id"] == rid


def test_runs_publish_fanout_counts(tmp_path):
    import time as _t

    from fastapi.testclient import TestClient
    from pytest_httpserver import HTTPServer

    from webclient.service import create_app

    html = "<html><body>" + "".join(f'<li class="r"><b>{i}</b></li>' for i in range(7)) + "</body></html>"
    with HTTPServer() as srv:
        srv.expect_request("/").respond_with_data(html, content_type="text/html")
        plan = {"root": "Reference", "steps": [
            {"kind": "get", "name": "resolve"}, {"kind": "call", "name": "resolve", "args": [], "kwargs": {}},
            {"kind": "get", "name": "select_all"}, {"kind": "call", "name": "select_all", "args": [{"value": "li.r"}], "kwargs": {}},
            {"kind": "get", "name": "extract"}, {"kind": "call", "name": "extract", "args": [], "kwargs": {"n": {"plan": {"root": "Document", "steps": [
                {"kind": "get", "name": "select"}, {"kind": "call", "name": "select", "args": [{"value": "b"}], "kwargs": {}},
                {"kind": "get", "name": "attr"}, {"kind": "call", "name": "attr", "args": [{"value": "text"}], "kwargs": {}}]}}}},
            {"kind": "get", "name": "project"}, {"kind": "call", "name": "project", "args": [], "kwargs": {}}]}
        with TestClient(create_app(traces_dir=tmp_path)) as client:
            rid = client.post("/runs", json={"plan": plan, "url": srv.url_for("/")}).json()["id"]
            for _ in range(100):
                got = client.get(f"/runs/{rid}").json()
                if got["status"] != "running":
                    break
                _t.sleep(0.05)
            fan = [e for e in got["events"] if e.get("topic") == "plan" and e.get("phase") == "fanout"]
            assert fan and fan[0]["detail"] == {"op": "select_all", "selector": "li.r", "n": 7}
            # the fan-out's width: 7 items, run up to the http concurrency at once
            par = [e for e in got["events"] if e.get("topic") == "plan" and e.get("phase") == "parallel"]
            assert par and par[0]["detail"]["n"] == 7 and 1 <= par[0]["detail"]["limit"] <= 7
            assert par[0]["detail"]["bound"] == "http"
            # every record ran as a fan-out ITEM: its events carry the index path, and each ends with plan.item
            ends = [e for e in got["events"] if e.get("topic") == "plan" and e.get("phase") == "item"]
            assert sorted(e["item"][0] for e in ends if e["detail"]["status"] == "ok") == list(range(7))
            steps = [e for e in got["events"] if e.get("phase") == "step" and e["detail"]["op"] == "select"]
            assert steps and all(len(e.get("item") or []) == 1 for e in steps)
            # the run settles before it says done: the last event is a pool sample back at idle
            last = got["events"][-1]
            assert last["topic"] == "resources" and last["http_free"] == last["http_total"]
            # ... with the process tree's memory (and CPU, once there is a previous sample)
            assert last["mem_mb"] > 0 and last["procs"] >= 1 and last.get("cpu_pct", 0) >= 0
            # every row event carries the row (a replay shows the rows as they arrived) and its index
            rows = [e for e in got["events"] if e.get("topic") == "plan" and e.get("phase") == "row"]
            assert sorted(e["detail"]["index"] for e in rows) == list(range(7))
            assert sorted(e["detail"]["row"]["n"] for e in rows) == [str(i) for i in range(7)]
            # the trace listing reports each trace's size on disk
            listed = {t["id"]: t for t in client.get("/traces").json()}
            assert listed[rid]["bytes"] > 0


def test_traces_can_be_deleted_one_or_all_but_kept(tmp_path):
    from fastapi.testclient import TestClient

    from webclient.service import create_app

    for name in ("a", "b", "keepme"):
        (tmp_path / f"{name}.jsonl").write_text('{"topic":"trace"}\n' * 3)
    with TestClient(create_app(traces_dir=tmp_path)) as client:
        assert {t["id"] for t in client.get("/traces").json()} == {"a", "b", "keepme"}
        r = client.delete("/traces/a").json()
        assert r["deleted"] and r["bytes"] > 0 and not (tmp_path / "a.jsonl").exists()
        assert client.delete("/traces/a").status_code == 404
        assert client.delete("/traces/../etc").status_code in (404, 405)  # no path escapes
        r = client.delete("/traces", params={"keep": "keepme"}).json()
        assert r["deleted"] == 1 and r["kept"] == ["keepme"]
        assert [t["id"] for t in client.get("/traces").json()] == ["keepme"]



def test_a_trace_serves_a_page_as_it_was(tmp_path):
    import time as _t

    from fastapi.testclient import TestClient
    from pytest_httpserver import HTTPServer

    from webclient.service import create_app

    with HTTPServer() as srv:
        srv.expect_request("/").respond_with_data("<html><body><li class='r'><b>x</b></li></body></html>", content_type="text/html")
        plan = {"root": "Reference", "steps": [
            {"kind": "get", "name": "resolve"}, {"kind": "call", "name": "resolve", "args": [], "kwargs": {}},
            {"kind": "get", "name": "select_all"}, {"kind": "call", "name": "select_all", "args": [{"value": "li.r"}], "kwargs": {}},
            {"kind": "get", "name": "extract"}, {"kind": "call", "name": "extract", "args": [], "kwargs": {"n": {"plan": {"root": "Document", "steps": [
                {"kind": "get", "name": "select"}, {"kind": "call", "name": "select", "args": [{"value": "b"}], "kwargs": {}},
                {"kind": "get", "name": "attr"}, {"kind": "call", "name": "attr", "args": [{"value": "text"}], "kwargs": {}}]}}}},
            {"kind": "get", "name": "project"}, {"kind": "call", "name": "project", "args": [], "kwargs": {}}]}
        with TestClient(create_app(traces_dir=tmp_path)) as client:
            rid = client.post("/runs", json={"plan": plan, "url": srv.url_for("/")}).json()["id"]
            for _ in range(100):
                got = client.get(f"/runs/{rid}").json()
                if got["status"] != "running":
                    break
                _t.sleep(0.05)
            step = next(e for e in got["events"] if e.get("phase") == "step" and e["detail"]["op"] == "select_all")
            page = client.get(f"/traces/{rid}/documents/{step['document_id']}").json()
            assert "<li class='r'>" in page["content"] or '<li class="r">' in page["content"]
            assert page["url"].startswith(srv.url_for("/")) and page["n"] <= step["n"]
            assert client.get(f"/traces/{rid}/documents/nope").status_code == 404


def test_a_runs_trace_holds_only_its_own_run(tmp_path):
    # the bus retains history; a run's trace starts at the run, not with earlier runs' events
    import time as _t

    from fastapi.testclient import TestClient
    from pytest_httpserver import HTTPServer

    from webclient.service import create_app

    html = "<html><body>" + "".join(f'<li class="r"><b>{i}</b></li>' for i in range(3)) + "</body></html>"
    with HTTPServer() as srv:
        srv.expect_request("/").respond_with_data(html, content_type="text/html")
        plan = {"root": "Reference", "steps": [
            {"kind": "get", "name": "resolve"}, {"kind": "call", "name": "resolve", "args": [], "kwargs": {}},
            {"kind": "get", "name": "select_all"}, {"kind": "call", "name": "select_all", "args": [{"value": "li.r"}], "kwargs": {}},
            {"kind": "get", "name": "extract"}, {"kind": "call", "name": "extract", "args": [], "kwargs": {"n": {"plan": {"root": "Document", "steps": [
                {"kind": "get", "name": "select"}, {"kind": "call", "name": "select", "args": [{"value": "b"}], "kwargs": {}},
                {"kind": "get", "name": "attr"}, {"kind": "call", "name": "attr", "args": [{"value": "text"}], "kwargs": {}}]}}}},
            {"kind": "get", "name": "project"}, {"kind": "call", "name": "project", "args": [], "kwargs": {}}]}
        with TestClient(create_app(traces_dir=tmp_path)) as client:
            ids = []
            for _ in range(2):
                rid = client.post("/runs", json={"plan": plan, "url": srv.url_for("/")}).json()["id"]
                for _ in range(100):
                    if client.get(f"/runs/{rid}").json()["status"] != "running":
                        break
                    _t.sleep(0.05)
                ids.append(rid)
            second = client.get(f"/traces/{ids[1]}/events").json()
            assert sum(1 for e in second if e.get("topic") == "plan" and e.get("phase") == "started") == 1
            assert sum(1 for e in second if e.get("topic") == "plan" and e.get("phase") == "row") == 3


def test_concurrent_runs_keep_their_own_traces(tmp_path):
    # two runs at once share the engine's bus; each run's trace (and live events) hold only its own
    import time as _t

    from fastapi.testclient import TestClient
    from pytest_httpserver import HTTPServer
    from werkzeug.wrappers import Response

    from webclient.service import create_app

    def slow(tag: str):
        def handler(_req):
            _t.sleep(0.3)  # both runs are in flight together
            body = "<html><body>" + "".join(f'<li class="r"><b>{tag}{i}</b></li>' for i in range(3)) + "</body></html>"
            return Response(body, content_type="text/html")
        return handler

    def plan() -> dict:
        return {"root": "Reference", "steps": [
            {"kind": "get", "name": "resolve"}, {"kind": "call", "name": "resolve", "args": [], "kwargs": {}},
            {"kind": "get", "name": "select_all"}, {"kind": "call", "name": "select_all", "args": [{"value": "li.r"}], "kwargs": {}},
            {"kind": "get", "name": "extract"}, {"kind": "call", "name": "extract", "args": [], "kwargs": {"n": {"plan": {"root": "Document", "steps": [
                {"kind": "get", "name": "select"}, {"kind": "call", "name": "select", "args": [{"value": "b"}], "kwargs": {}},
                {"kind": "get", "name": "attr"}, {"kind": "call", "name": "attr", "args": [{"value": "text"}], "kwargs": {}}]}}}},
            {"kind": "get", "name": "project"}, {"kind": "call", "name": "project", "args": [], "kwargs": {}}]}

    with HTTPServer() as srv:
        srv.expect_request("/a").respond_with_handler(slow("a"))
        srv.expect_request("/b").respond_with_handler(slow("b"))
        with TestClient(create_app(traces_dir=tmp_path)) as client:
            ids = {tag: client.post("/runs", json={"plan": plan(), "url": srv.url_for(f"/{tag}")}).json()["id"] for tag in "ab"}
            for rid in ids.values():
                for _ in range(200):
                    if client.get(f"/runs/{rid}").json()["status"] != "running":
                        break
                    _t.sleep(0.05)
            for tag, rid in ids.items():
                other = "b" if tag == "a" else "a"
                for events in (client.get(f"/traces/{rid}/events").json(), client.get(f"/runs/{rid}").json()["events"]):
                    plan_events = [e for e in events if e.get("topic") == "plan"]
                    assert sum(1 for e in plan_events if e.get("phase") == "started") == 1
                    rows = [e for e in plan_events if e.get("phase") == "row"]
                    assert len(rows) == 3
                    assert all(f"{tag}" in str(e["detail"]) and f"'{other}0'" not in str(e["detail"]) for e in rows)
                    assert all(e.get("run_id") in (None, rid) for e in events)  # resources samples carry none
                    assert f"/{other}" not in " ".join(str(e.get("url", "")) for e in events)


def test_the_bus_stamps_the_run_and_attributes_a_pages_later_events_to_it():
    from webclient.events import EventBus, run_scope
    from webclient.models import Event

    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe("", seen.append, run_id="r1")
    with run_scope("r1"):
        bus.publish(Event(topic="x", document_id="d1"))
    bus.publish(Event(topic="y", document_id="d1"))  # e.g. a browser callback, outside the run's context
    bus.publish(Event(topic="z", document_id="d2"))  # another page: not this run's
    with run_scope("r2"):
        bus.publish(Event(topic="w"))
    assert [e.topic for e in seen] == ["x", "y"]
    assert [e.topic for e in bus.since(0, run_id="r1")] == ["x", "y"]


def test_the_engine_loop_carries_the_run_into_its_coroutines():
    import asyncio

    from webclient.core.client.loop import EngineLoop
    from webclient.events import CURRENT_RUN, run_scope

    loop = EngineLoop()
    try:
        async def which() -> "str | None":
            await asyncio.sleep(0)
            return CURRENT_RUN.get()

        with run_scope("r7"):
            assert loop.run(which()) == "r7"
            assert loop.submit(which()).result() == "r7"

            async def gen():
                yield CURRENT_RUN.get()

            assert list(loop.stream(gen())) == ["r7"]
        assert loop.run(which()) is None
    finally:
        loop.stop()


def test_one_trace_ending_does_not_stop_capture_for_another(tmp_path):
    # two runs trace at once on one engine: the first to finish must not switch capture off for the other
    from webclient import WebClient

    wc = WebClient()
    try:
        engine = wc._the_engine()
        a = wc.trace(tmp_path / "a.jsonl")
        b = wc.trace(tmp_path / "b.jsonl")
        assert engine.tracing
        a.close()
        assert engine.tracing and engine.trace_path == str(b.path)
        b.close()
        assert not engine.tracing
    finally:
        wc.close()
