"""web.kernel unit tests -- the layer in isolation (it imports nothing but pydantic)."""

from __future__ import annotations

from web.kernel import Event, EventBus, WebError, WebException, err


def test_weberror_is_data_and_exception_carries_it() -> None:
    e = err("http.timeout", "no response", url="http://x/")
    assert e.code == "http.timeout" and e.detail["url"] == "http://x/"
    assert str(e) == "http.timeout: no response"
    exc = WebException(e)
    assert exc.error is e
    # a bare-code form also works
    assert WebException("parse.not_html").error == WebError(code="parse.not_html")


def test_bus_delivers_by_prefix_and_swallows_handler_errors() -> None:
    bus = EventBus()
    seen: list[str] = []
    bus.subscribe("fetch", lambda ev: seen.append(ev.topic))
    bus.subscribe("", lambda ev: (_ for _ in ()).throw(RuntimeError("boom")))  # never breaks emit
    all_seen: list[str] = []
    sub = bus.subscribe("", lambda ev: all_seen.append(ev.topic))

    bus.publish(Event(topic="fetch.retry"))
    bus.publish(Event(topic="parse"))
    assert seen == ["fetch.retry"]  # prefix match only
    assert all_seen == ["fetch.retry", "parse"]  # "" sees everything
    sub()  # unsubscribe
    bus.publish(Event(topic="fetch"))
    assert all_seen == ["fetch.retry", "parse"]  # no longer delivered
