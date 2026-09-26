"""Every event of a running plan is attached to the plan step that caused it (``Event.step``), and
every step publishes its result -- so a run view is built from the plan and the stream, not guessed."""
import time

from fastapi.testclient import TestClient
from pytest_httpserver import HTTPServer

from webclient.service import create_app


def _read(tag: str, attr: str = "text") -> dict:
    return {"plan": {"root": "Document", "steps": [
        {"kind": "get", "name": "select"}, {"kind": "call", "name": "select", "args": [{"value": tag}], "kwargs": {}},
        {"kind": "get", "name": "attr"}, {"kind": "call", "name": "attr", "args": [{"value": attr}], "kwargs": {}}]}}


PLAN = {"root": "Reference", "steps": [
    {"kind": "get", "name": "resolve"}, {"kind": "call", "name": "resolve", "args": [], "kwargs": {}},
    {"kind": "get", "name": "select_all"}, {"kind": "call", "name": "select_all", "args": [{"value": "li.r"}], "kwargs": {}},
    {"kind": "get", "name": "extract"}, {"kind": "call", "name": "extract", "args": [], "kwargs": {
        "n": _read("b"),
        "detail": {"plan": {"root": "Document", "steps": [
            *_read("a", "href")["plan"]["steps"],
            {"kind": "get", "name": "resolve"}, {"kind": "call", "name": "resolve", "args": [], "kwargs": {}},
            *_read("p.desc")["plan"]["steps"]]}}}},
    {"kind": "get", "name": "project"}, {"kind": "call", "name": "project", "args": [], "kwargs": {}}]}


def test_every_event_carries_its_plan_step_and_each_step_its_result(tmp_path):
    html = "<html><body>" + "".join(f'<li class="r"><b>{i}</b><a href="/d{i}">x</a></li>' for i in range(2)) + "</body></html>"
    with HTTPServer() as srv:
        srv.expect_request("/").respond_with_data(html, content_type="text/html")
        for i in range(2):
            srv.expect_request(f"/d{i}").respond_with_data(f"<p class='desc'>about {i}</p>", content_type="text/html")
        with TestClient(create_app(traces_dir=tmp_path)) as client:
            rid = client.post("/runs", json={"plan": PLAN, "url": srv.url_for("/")}).json()["id"]
            for _ in range(200):
                if client.get(f"/runs/{rid}").json()["status"] != "running":
                    break
                time.sleep(0.05)
            events = client.get(f"/traces/{rid}/events").json()

    results = {(e["step"], tuple(e.get("item") or [])): e["detail"] for e in events if e.get("phase") == "result"}
    # the root resolve fetched the page; its fetch is attached to step 0
    assert results[("0", ())]["kind"] == "Document"
    assert any(e["topic"] == "network.navigation" and e["step"] == "0" for e in events)
    # the fan-out: 2 records
    assert results[("2", ())]["kind"] == "Collection" and results[("2", ())]["n"] == 2
    # a column's steps, per item, addressed inside the extract's kwarg
    assert results[("4/kw:n/2", (1,))]["preview"] == "1"
    # the detail page each record resolved -- its own page, fetched under the column's resolve step
    detail = results[("4/kw:detail/4", (0,))]
    assert detail["kind"] == "Document" and detail["document_id"]
    assert any(e["topic"] == "network.navigation" and e["step"] == "4/kw:detail/4" and e.get("item") == [0] for e in events)
    assert results[("4/kw:detail/8", (0,))]["preview"] == "about 0"
    # every step result says how long it took
    assert all("ms" in d and d["ok"] for d in results.values())


def test_a_failing_step_publishes_a_failed_result_at_its_address(tmp_path):
    plan = {"root": "Reference", "steps": [
        {"kind": "get", "name": "resolve"}, {"kind": "call", "name": "resolve", "args": [], "kwargs": {}},
        {"kind": "get", "name": "select"}, {"kind": "call", "name": "select", "args": [{"value": ".missing"}], "kwargs": {}}]}
    with HTTPServer() as srv:
        srv.expect_request("/").respond_with_data("<p>hi</p>", content_type="text/html")
        with TestClient(create_app(traces_dir=tmp_path)) as client:
            rid = client.post("/runs", json={"plan": plan, "url": srv.url_for("/")}).json()["id"]
            for _ in range(200):
                if client.get(f"/runs/{rid}").json()["status"] != "running":
                    break
                time.sleep(0.05)
            events = client.get(f"/traces/{rid}/events").json()
    failed = [e for e in events if e.get("phase") == "result" and not e["detail"]["ok"]]
    assert failed and failed[0]["step"] == "2" and failed[0]["detail"]["op"] == "select" and failed[0]["detail"]["error"]
    assert any(e["topic"] == "error" and e.get("step") == "2" for e in events)  # the error is at the step too


def test_a_recorded_call_is_attached_to_its_step_in_the_recorded_plan(tmp_path):
    # everything is a plan: imperative calls under wc.record() become the recording's plan, and what
    # they publish is attached to the step they became -- a recorded trace replays like an executed one
    from webclient import WebClient

    with HTTPServer() as srv:
        srv.expect_request("/").respond_with_data("<p>hi</p>", content_type="text/html")
        with WebClient() as wc:
            seen: list = []
            wc.bus.subscribe("", seen.append)
            with wc.record() as rec:
                rec.fetch(srv.url_for("/"))
                plan = rec.plan
    assert plan is not None and plan._plan.steps[0].name == "resolve"
    at0 = [e for e in seen if e.step == "0"]
    assert any(e.topic == "network.navigation" for e in at0)
    assert any(getattr(e, "phase", None) == "step" and e.detail.get("recorded") for e in at0)
    result = next(e for e in at0 if getattr(e, "phase", None) == "result")
    assert result.detail["kind"] == "Document" and result.detail["ok"]


def test_every_event_carries_the_plan_it_belongs_to(tmp_path):
    # a trace can hold several plans' runs: each event says which plan (its id), a sub-plan's are its plan's
    from webclient.query.expr import from_plan

    with HTTPServer() as srv:
        html = "<html><body>" + "".join(f'<li class="r"><b>{i}</b><a href="/d{i}">x</a></li>' for i in range(2)) + "</body></html>"
        srv.expect_request("/").respond_with_data(html, content_type="text/html")
        for i in range(2):
            srv.expect_request(f"/d{i}").respond_with_data(f"<p class='desc'>about {i}</p>", content_type="text/html")
        with TestClient(create_app(traces_dir=tmp_path)) as client:
            started = client.post("/runs", json={"plan": PLAN, "url": srv.url_for("/")}).json()
            for _ in range(200):
                if client.get(f"/runs/{started['id']}").json()["status"] != "running":
                    break
                time.sleep(0.05)
            events = client.get(f"/traces/{started['id']}/events").json()
            meta = client.get(f"/traces/{started['id']}/plan").json()
    pid = from_plan(PLAN, None)._plan.id
    assert started["plan_id"] == pid and meta["plan_id"] == pid
    stepped = [e for e in events if e.get("step")]
    assert stepped and all(e.get("plan_id") == pid for e in stepped)  # the nested columns' too


def test_an_imperative_loop_is_recorded_as_the_plan_that_does_it(tmp_path):
    # everything is a plan: a script that loops over the cards reading each (and following each card's link)
    # becomes select_all(...).extract(...).project(); the trace carries it, and every read is placed on it
    from webclient import WebClient
    from webclient.query.expr import from_blob
    from webclient.trace import read

    html = "<html><body>" + "".join(f'<li class="r"><b>{i}</b><a href="/d{i}">x</a></li>' for i in range(3)) + "</body></html>"
    path = tmp_path / "rec.jsonl"
    with HTTPServer() as srv:
        srv.expect_request("/").respond_with_data(html, content_type="text/html")
        for i in range(3):
            srv.expect_request(f"/d{i}").respond_with_data(f"<p class='desc'>about {i}</p>", content_type="text/html")
        with WebClient() as wc:
            with wc.trace(path), wc.record() as rec:
                page = rec.fetch(srv.url_for("/"))
                seen = []
                for card in page.select_all("li.r"):
                    title = card.select("b").attr("text")
                    about = card.select("a").attr("href").resolve().select("p.desc").attr("text")
                    seen.append({"title": title, "about": about})
                plan = rec.reads_plan
            described = plan.describe()
            assert ".select_all('li.r').extract(" in described and described.endswith(".project()")
            rows = plan.collect()  # the plan does what the loop did
            assert [{"t": r["b_text"], "a": r["p_desc_text"]} for r in rows] == [{"t": s["title"], "a": s["about"]} for s in seen]

    reader = read(path)
    assert from_blob(reader.plan_blob, None).describe() == described
    steps = reader.plan_steps
    reads = [e for e in reader.events if getattr(e, "phase", None) == "result" and (e.step or "").startswith("@")]
    assert reads and all(e.step in steps for e in reads)
    # item 2's detail page was opened by the resolve of the extract's column, for item 2
    resolved = [e for e in reads if e.detail.get("op") == "resolve" and e.item == [2]]
    assert resolved and steps[resolved[0].step].startswith("4/kw:p_desc_text/")
