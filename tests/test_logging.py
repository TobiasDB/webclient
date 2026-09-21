"""Logging: standard ``logging``, a NullHandler on the package root, zero cost when off, and
the one-way bus -> log bridge (roadmap N18 / D7)."""

import logging

import pytest

from webclient import EventBus, NetworkEvent, WebClient, configure_logging, log_events


def test_package_root_has_a_null_handler_and_no_others():
    root = logging.getLogger("webclient")
    kinds = [type(h) for h in root.handlers if not getattr(h, "_webclient_configured", False)]
    assert logging.NullHandler in kinds


def test_configure_logging_is_idempotent(capsys):
    logger = configure_logging("DEBUG")
    n = len(logger.handlers)
    configure_logging("INFO")  # re-levels the same handler, never stacks a second
    assert len(logger.handlers) == n
    assert logger.level == logging.INFO
    with pytest.raises(ValueError):
        configure_logging("LOUD")
    configure_logging(None)  # leaves everything alone


def test_fetch_narrates_the_hot_path_at_debug(httpserver, caplog):
    httpserver.expect_request("/p").respond_with_data("<html><title>t</title></html>",
                                                      content_type="text/html")
    with caplog.at_level(logging.DEBUG, logger="webclient"), WebClient() as wc:
        wc.fetch(httpserver.url_for("/p"))
    names = {r.name for r in caplog.records}
    assert "webclient.clients.http" in names and "webclient.clients.pool" in names
    line = next(r.getMessage() for r in caplog.records if r.name == "webclient.clients.http")
    assert "GET" in line and "-> 200" in line and "html" in line


def test_nothing_is_logged_when_off(httpserver, caplog):
    httpserver.expect_request("/q").respond_with_data("ok")
    caplog.set_level(logging.WARNING, logger="webclient")
    with WebClient() as wc:
        wc.fetch(httpserver.url_for("/q"))
    assert not [r for r in caplog.records if r.name.startswith("webclient")]


def test_log_events_bridges_the_bus_one_way(caplog):
    bus = EventBus()
    sub = log_events(bus)
    with caplog.at_level(logging.DEBUG, logger="webclient.events"):
        bus.publish(NetworkEvent(status_code=200, document_id="d1"))
    msgs = [r.getMessage() for r in caplog.records if r.name == "webclient.events"]
    assert msgs and msgs[0].startswith("network #1 doc=d1") and "status_code=200" in msgs[0]
    sub.cancel()
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="webclient.events"):
        bus.publish(NetworkEvent(status_code=500))
    assert not [r for r in caplog.records if r.name == "webclient.events"]
