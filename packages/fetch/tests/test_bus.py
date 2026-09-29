"""web.kernel unit tests -- the layer in isolation (it imports nothing but pydantic)."""

from __future__ import annotations

from pydantic import BaseModel
from web.fetch import Event, EventBus, WebError, WebException, err


class _Ev(BaseModel):
    """A layer's event is a plain model with a topic -- it does NOT inherit a kernel base; the bus
    routes it structurally (it satisfies the :class:`~web.kernel.Event` Protocol)."""

    topic: str


def test_layer_event_satisfies_the_event_protocol_without_inheriting() -> None:
    assert isinstance(
        _Ev(topic="fetch"), Event
    )  # runtime_checkable Protocol: just needs `topic`
    assert (
        Event not in _Ev.__mro__
    )  # and it does NOT inherit the kernel -- structural only


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
    bus.subscribe(
        "", lambda ev: (_ for _ in ()).throw(RuntimeError("boom"))
    )  # never breaks emit
    all_seen: list[str] = []
    sub = bus.subscribe("", lambda ev: all_seen.append(ev.topic))

    bus.publish(_Ev(topic="fetch.retry"))
    bus.publish(_Ev(topic="parse"))
    assert seen == ["fetch.retry"]  # prefix match only
    assert all_seen == ["fetch.retry", "parse"]  # "" sees everything
    sub()  # unsubscribe
    bus.publish(_Ev(topic="fetch"))
    assert all_seen == ["fetch.retry", "parse"]  # no longer delivered


def test_emit_is_noop_without_a_trace_and_captured_within_one() -> None:
    from web.fetch import Trace, emit

    emit(_Ev(topic="x"))  # no active trace -> no-op, no error
    with Trace() as t:
        emit(_Ev(topic="fetch"))
        emit(_Ev(topic="crawl"))
    assert [e.topic for e in t.events] == ["fetch", "crawl"]
    emit(_Ev(topic="after"))  # outside the scope -> not captured
    assert [e.topic for e in t.events] == ["fetch", "crawl"]
