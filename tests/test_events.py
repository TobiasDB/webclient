from webclient import (
    ActionEvent,
    ConsoleEvent,
    DOMUpdateEvent,
    Event,
    EventBus,
    EventRegistry,
    NetworkEvent,
    XHREvent,
)


def test_bus_topic_prefix_matching():
    bus = EventBus()
    seen = []
    bus.subscribe("dom", seen.append)
    bus.publish(DOMUpdateEvent(kind="added"))
    bus.publish(ActionEvent(action="click"))
    assert [e.topic for e in seen] == ["dom.update"]


def test_bus_empty_topic_matches_everything():
    bus = EventBus()
    seen = []
    bus.subscribe("", seen.append)
    bus.publish(ActionEvent(action="click"))
    bus.publish(DOMUpdateEvent(kind="added"))
    assert len(seen) == 2


def test_bus_correlation_filter():
    bus = EventBus()
    seen = []
    bus.subscribe("", seen.append, document_id="d1")
    bus.publish(ActionEvent(action="a", document_id="d1"))
    bus.publish(ActionEvent(action="b", document_id="d2"))
    assert [e.action for e in seen] == ["a"]


def test_bus_stamps_seq_per_document_and_ts():
    bus = EventBus()
    e1 = ActionEvent(action="a", document_id="d1")
    e2 = ActionEvent(action="b", document_id="d1")
    e3 = ActionEvent(action="c", document_id="d2")
    for e in (e1, e2, e3):
        bus.publish(e)
    assert (e1.seq, e2.seq, e3.seq) == (1, 2, 1)
    assert all(e.ts is not None for e in (e1, e2, e3))


def test_bus_cancel():
    bus = EventBus()
    seen = []
    sub = bus.subscribe("", seen.append)
    sub.cancel()
    bus.publish(ActionEvent(action="a"))
    assert seen == []


def test_registry_resolves_exact_and_ancestors():
    registry = EventRegistry()
    assert registry.resolve("network.xhr") is XHREvent
    assert registry.resolve("network.xhr.slow") is XHREvent
    assert registry.resolve("rrweb.dom.update") is DOMUpdateEvent  # leading drop
    assert registry.resolve("console") is ConsoleEvent
    assert registry.resolve("completely.unknown") is Event


def test_registry_registers_plugin_events():
    class RRWebEvent(DOMUpdateEvent):
        topic: str = "rrweb.dom.update"

    registry = EventRegistry()
    registry.register(RRWebEvent)
    assert registry.resolve("rrweb.dom.update") is RRWebEvent
    assert registry.resolve("network") is NetworkEvent
