"""M4 browser tests. One WebClient (one chromium) for the whole module."""
import pytest

from webclient import (
    ActionEvent,
    BinaryDocument,
    ConsoleEvent,
    DOMSnapshotEvent,
    DOMUpdateEvent,
    Event,
    LiveDocument,
    Plugin,
    Reference,
    Script,
    WebClient,
    XHREvent,
)

APP = """
<html><head><title>App</title></head><body>
  <div class="card" id="c1"><h2>Card One</h2>
    <button onclick="this.parentElement.insertAdjacentHTML('beforeend',
      '<div class=added>added-one</div>')">grow</button></div>
  <div class="card" id="c2"><h2>Card Two</h2></div>
  <form><input id="name" type="text"><span id="out"></span></form>
  <a id="go" href="/two">two</a>
  <script>
    console.log("booted");
    document.querySelector("#name").addEventListener("input",
      e => document.querySelector("#out").textContent = e.target.value);
    const xhr = new XMLHttpRequest();
    xhr.open("GET", "/api"); xhr.send();
  </script>
</body></html>
"""

PAGE_TWO = "<html><body><h1>Second</h1></body></html>"


@pytest.fixture(scope="module")
def wc():
    with WebClient(timeout=10.0) as client:
        yield client


@pytest.fixture
def app(httpserver, wc):
    httpserver.expect_request("/app").respond_with_data(
        APP, content_type="text/html")
    httpserver.expect_request("/two").respond_with_data(
        PAGE_TWO, content_type="text/html")
    httpserver.expect_request("/api").respond_with_json({"ok": True})
    live = wc.ref(httpserver.url_for("/app")).fetch(browser=True)
    yield live
    wc.release(live)


def test_browser_fetch_returns_live_document(app, wc):
    assert isinstance(app, LiveDocument)
    assert app.ok and app.kind == "html"
    assert app.select("h2").text == "Card One"
    assert app.select('//div[@id="c2"]/h2').text == "Card Two"   # xpath


def test_click_mutates_dom_and_records_everything(app):
    app.click("#c1 button")
    app.wait_for(".added", timeout=5.0)
    assert app.select(".added").text == "added-one"
    assert [a.action for a in app.actions if a.action == "click"] == ["click"]
    assert any(isinstance(e, DOMUpdateEvent) for e in app.dom_mutations)


def test_write_and_live_state(app):
    app.write("#name", "Ada")
    assert app.select("#out").text == "Ada"
    assert app.evaluate("document.querySelector('#name').value") == "Ada"


def test_xhr_and_console_capture(app):
    app.wait_for(timeout=0.3)        # let the boot script finish
    assert any(e.request.path == "/api" for e in app.xhr_requests)
    assert any("booted" in e.text for e in app.console)
    assert all(isinstance(e, XHREvent) for e in app.xhr_requests)


def test_dom_snapshot_checkpoint_with_digest(app):
    snapshots = app.events_of(DOMSnapshotEvent)
    assert snapshots, "load should emit an initial snapshot"
    first = snapshots[0]
    assert first.digest and "html" in first.snapshot
    assert first.seq is not None


def test_livenode_event_narrowing(app):
    app.click("#c1 button")
    app.wait_for(".added", timeout=5.0)
    card1 = app.select("#c1")
    card2 = app.select("#c2")
    assert card1.identity_path is not None
    assert len(card1.events_of(DOMUpdateEvent)) > 0
    assert len(card2.events_of(DOMUpdateEvent)) == 0     # sibling untouched


def test_missing_targets_are_loud_unless_optional(app):
    with pytest.raises(LookupError):
        app.click(".nope", timeout=0.3)
    app.click(".nope", timeout=0.3, optional=True)        # no raise
    with pytest.raises(LookupError):
        app.select(".nope")
    assert app.select(".nope", optional=True) is None


def test_screenshot(app):
    shot = app.screenshot()
    assert isinstance(shot, BinaryDocument)
    assert shot.content[:8] == b"\x89PNG\r\n\x1a\n"
    element_shot = app.screenshot("#c1")
    assert element_shot.content[:4] == b"\x89PNG"


def test_navigate_returns_new_document_old_refuses(app, httpserver):
    new = app.navigate(httpserver.url_for("/two"))
    try:
        assert new.select("h1").text == "Second"
        assert new.id != app.id
        with pytest.raises(RuntimeError, match="navigated"):
            app.click("a")
    finally:
        new._client.release(new)


def test_replay_reproduces_state(httpserver, wc):
    httpserver.expect_request("/app2").respond_with_data(
        APP, content_type="text/html")
    live = wc.ref(httpserver.url_for("/app2")).fetch(browser=True)
    live.click("#c1 button").write("#name", "Bob")
    recording = list(live.actions)
    wc.release(live)

    fresh = wc.ref(httpserver.url_for("/app2")).fetch(browser=True)
    fresh.replay(recording)
    try:
        assert fresh.select(".added", optional=True) is not None
        assert fresh.select("#out").text == "Bob"
    finally:
        wc.release(fresh)


def test_session_storage_state_persists(httpserver, wc):
    httpserver.expect_request("/store").respond_with_data(
        "<html><body>store</body></html>", content_type="text/html")
    session = wc.session()
    live = session.ref(httpserver.url_for("/store")).fetch(browser=True)
    live.execute("localStorage.setItem('k', 'v1')")
    session.close()
    assert session.storage_state is not None
    origins = session.storage_state.get("origins", [])
    stored = [item for origin in origins
              for item in origin.get("localStorage", [])]
    assert {"name": "k", "value": "v1"} in stored


def test_release_returns_page_to_pool(httpserver, wc):
    httpserver.expect_request("/p").respond_with_data(
        "<html><body>p</body></html>", content_type="text/html")
    live = wc.ref(httpserver.url_for("/p")).fetch(browser=True)
    held = len(wc.pool._pages_held)
    wc.release(live)
    assert len(wc.pool._pages_held) == held - 1
    with pytest.raises(RuntimeError, match="released"):
        live.click("body")


def test_custom_page_plugin_needs_no_engine_changes(httpserver, wc):
    """The M4 acceptance gate: a plugin with its own script, binding and
    custom event lands events on the document via registration alone."""

    class PingEvent(Event):
        topic: str = "custom.ping"
        payload: str = ""

    class PingPlugin(Plugin):
        name: str = "ping"
        surfaces: list = ["page"]
        events: list = [PingEvent]
        scripts: list = [Script(source="console.log('__ping__:hello')")]

        def attach(self, surface):
            def on_console(message):
                if message.text.startswith("__ping__:"):
                    surface.emit(PingEvent(payload=message.text.split(":")[1]))
            surface.raw.on("console", on_console)
            self._handles = (surface.raw, on_console)

        def detach(self, surface):
            page, handler = self._handles
            page.remove_listener("console", handler)

    wc.use(PingPlugin())
    httpserver.expect_request("/ping").respond_with_data(
        "<html><body>ping</body></html>", content_type="text/html")
    live = wc.ref(httpserver.url_for("/ping")).fetch(browser=True)
    try:
        pings = live.events_of("custom.ping")
        assert len(pings) == 1
        assert pings[0].payload == "hello"
        assert pings[0].source == "ping"
        assert wc.registry.resolve("custom.ping") is PingEvent
    finally:
        wc.release(live)
