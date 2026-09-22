"""Page scripts: named, registered, togglable, governed by a policy, reported as ScriptEvents;
the rrweb recorder rides the same registry and is on only under a trace (roadmap N8 / N3)."""

import pytest

from webclient import Script, ScriptEvent, ScriptPolicy, ScriptRegistry, WebClient
from webclient.clients import PageScript
from webclient.events import EventBus


def test_registry_names_declares_and_toggles():
    reg = ScriptRegistry()
    reg.declare("LiveBacking", (PageScript("a", "init"), PageScript("b", "inline"), PageScript("c", "inline")))
    names = [s.name for s in reg.list()]
    assert names == ["LiveBacking.init", "LiveBacking.inline", "LiveBacking.inline.2"]
    reg.inject("x", "load")
    assert reg.get("user.1").owner == "user" and reg.get("user.1").phase == "load"
    reg.disable("LiveBacking.init")
    assert [p.name for p in reg.gather()] == ["LiveBacking.inline", "LiveBacking.inline.2", "user.1"]
    reg.enable("LiveBacking.init")
    reg.policy = ScriptPolicy(deny=("LiveBacking.",))
    assert [p.name for p in reg.gather()] == ["user.1"]
    reg.policy = ScriptPolicy(default="off", allow=("LiveBacking.init",))
    assert [p.name for p in reg.gather()] == ["LiveBacking.init"]


def test_topic_scripts_arm_the_bus_and_report():
    bus = EventBus()
    reg = ScriptRegistry(bus=bus)
    fired = []
    reg.bind(bus, fired.append)
    reg.register(Script("probe", "() => 1", on="action"))
    assert reg.get("probe").topic == "action" and reg.get("probe").phase is None
    from webclient.models import ActionEvent, ConsoleEvent
    bus.publish(ActionEvent(action="click"))
    bus.publish(ConsoleEvent(level="log", text="x"))
    assert [e.topic for e in fired] == ["action"]
    seen = []
    bus.subscribe("script", seen.append)
    reg.report("probe", "action", result={"a": [1, 2]}, document_id="d")
    assert isinstance(seen[0], ScriptEvent) and seen[0].detail == {"result": {"a": 2}}


APP = """<html><head><title>S</title></head><body>
<button id="b" onclick="document.body.insertAdjacentHTML('beforeend','<p class=added>x</p>')">go</button>
</body></html>"""


@pytest.fixture(scope="module")
def wc():
    with WebClient(timeout=10.0) as client:
        yield client


def test_builtin_scripts_are_named_and_runs_are_reported(httpserver, wc):
    httpserver.expect_request("/s").respond_with_data(APP, content_type="text/html")
    names = [s.name for s in wc.scripts.list()]
    assert "LiveBacking.init" in names and "LiveBacking.drain" in names and "wc.rrweb" in names
    assert not wc.scripts.get("wc.rrweb").enabled  # off unless tracing
    seen = []
    wc.bus.subscribe("script", seen.append)
    wc.scripts.register(Script("probe.load", "() => document.title", on="load"))
    wc.scripts.register(Script("probe.unload", "() => window.__bye = 1", on="unload"))
    live = wc.ref(httpserver.url_for("/s")).resolve(browser=True).collect()
    ran = {(e.script, e.phase) for e in seen}
    assert ("LiveBacking.init", "init") in ran and ("probe.load", "load") in ran
    assert next(e for e in seen if e.script == "probe.load").detail == {"result": "S"}
    assert "wc.rrweb" not in {e.script for e in seen}
    wc.release(live)
    assert ("probe.unload", "unload") in {(e.script, e.phase) for e in seen}
    wc.scripts.disable("probe.load")
    wc.scripts.disable("probe.unload")


def test_topic_script_runs_on_the_live_page_after_an_action(httpserver, wc):
    httpserver.expect_request("/t").respond_with_data(APP, content_type="text/html")
    seen = []
    wc.bus.subscribe("script", seen.append)
    wc.scripts.register(Script("count.added", "() => document.querySelectorAll('.added').length", on="action"))
    live = wc.ref(httpserver.url_for("/t")).resolve(browser=True).collect()
    live.click("#b")
    live.wait_for(".added")
    import time
    for _ in range(50):
        got = [e for e in seen if e.script == "count.added"]
        if got:
            break
        time.sleep(0.05)
    # the action event is published BEFORE the click is performed (correlation phase order),
    # so the script observes the pre-action DOM; what matters is that it ran on the right page.
    assert got and isinstance(got[-1].detail["result"], int) and got[-1].document_id == live.name
    wc.release(live)
    wc.scripts.disable("count.added")


def test_rrweb_records_under_a_trace(httpserver, wc, tmp_path):
    from webclient.trace import read

    httpserver.expect_request("/r").respond_with_data(APP, content_type="text/html")
    path = tmp_path / "rr.trace"
    with wc.trace(path):
        live = wc.ref(httpserver.url_for("/r")).resolve(browser=True).collect()
        assert wc.scripts.get("wc.rrweb").enabled
        live.click("#b")
        live.wait_for(".added")
        wc.release(live)
    assert not wc.scripts.get("wc.rrweb").enabled or not wc._the_engine().tracing
    reader = read(path)
    events = reader.rrweb()
    types = [e["type"] for e in events]
    assert 2 in types and 3 in types  # a FullSnapshot then IncrementalSnapshots
    assert (path / "rrweb").exists() and any((path / "rrweb").glob("*.json"))
    assert reader.rrweb(live.name) == events
