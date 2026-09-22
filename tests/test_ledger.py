"""The error ledger: no error disappears -- raised, returned under RETURN, or swallowed, every
WebError is published as an ErrorEvent bound to its op/subject and lands on ``doc.errors`` /
``wc.errors`` (roadmap N5 part 2)."""

import pytest

from webclient import RETURN, ErrorEvent, LoopEvent, WebClient, WebException
from webclient.events import EventBus, EventRegistry
from webclient.models import ActionEvent


def test_bus_history_and_since():
    bus = EventBus(history=3)
    for i in range(5):
        bus.publish(ActionEvent(action=str(i)))
    assert bus.cursor == 5
    assert [e.n for e in bus.since(0)] == [3, 4, 5]  # bounded by history
    assert [e.n for e in bus.since(4)] == [5]
    assert bus.since(5) == []


def test_registry_loads_and_upcasts_by_version():
    reg = EventRegistry()

    class LoopV2(LoopEvent):
        version: int = 2

    reg.register(LoopV2)

    @reg.upcaster("loop", 1)
    def _lift(data):
        data["loop"] = data.pop("name", "")  # v1 called the field "name"
        return data

    ev = reg.load({"topic": "loop", "version": 1, "name": "crawl", "phase": "done"})
    assert isinstance(ev, LoopV2) and ev.loop == "crawl" and ev.version == 2
    ev2 = reg.load({"topic": "loop", "version": 2, "loop": "x"})  # already current
    assert ev2.loop == "x"


def test_a_raised_select_miss_is_on_the_ledger_once(httpserver):
    httpserver.expect_request("/a").respond_with_data("<html><body><p>x</p></body></html>",
                                                      content_type="text/html")
    with WebClient() as wc:
        seen = []
        wc.bus.subscribe("error", seen.append)
        doc = wc.fetch(httpserver.url_for("/a"))
        with pytest.raises(LookupError):
            doc.select(".nope")
        assert len(seen) == 1 and isinstance(seen[0], ErrorEvent)
        err = seen[0].error
        assert err.code == "select.no_match" and err.op == "select" and err.subject == doc.name
        assert seen[0].raised is True and seen[0].document_id == doc.name
        assert [e.code for e in doc.errors] == ["select.no_match"]
        assert wc.errors[-1] is seen[0]


def test_a_returned_miss_is_on_the_ledger_too(httpserver):
    httpserver.expect_request("/b").respond_with_data("<html><body><p>x</p></body></html>",
                                                      content_type="text/html")
    with WebClient() as wc:
        doc = wc.fetch(httpserver.url_for("/b"))
        miss = doc.select(".nope", error=RETURN)
        assert not miss.ok
        assert wc.errors and wc.errors[-1].raised is False
        assert wc.errors[-1].error.code == "select.no_match"
        assert doc.errors[0].op == "select"


def test_a_not_ok_fetch_is_on_the_ledger(httpserver):
    httpserver.expect_request("/c").respond_with_data("nope", status=404)
    with WebClient() as wc:
        doc = wc.fetch(httpserver.url_for("/c"), optional=True)
        assert doc.errors[0].code == "fetch.http_status" and doc.errors[0].subject == doc.name
        assert wc.errors[-1].error.op == "fetch"
        with pytest.raises(WebException):
            wc.fetch(httpserver.url_for("/c"))
        assert len([e for e in wc.errors if e.error.code == "fetch.http_status"]) == 2


def test_crawl_failures_enter_the_ledger(httpserver):
    httpserver.expect_request("/").respond_with_data(
        '<html><body><a href="/dead">x</a></body></html>', content_type="text/html")
    httpserver.expect_request("/dead").respond_with_data("gone", status=500)
    with WebClient() as wc:
        with wc.crawl(httpserver.url_for("/"), auto=True, max_pages=5, obey_robots=False,
                      browser=False) as crawl:
            crawl.run()
        codes = [e.error.code for e in wc.errors]
        assert "fetch.http_status" in codes
        assert any(e.error.op == "fetch" for e in wc.errors)
