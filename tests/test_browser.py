"""M4 browser tests (MVP subset). One WebClient (one chromium) for the module.

Covers the implemented live surface: live fetch, interaction (click/write/
wait_for), live selection (css + xpath), console + DOM-mutation capture,
per-element event narrowing, missing-target policy, screenshot, reload replay,
and page release. (xhr capture, DOM snapshots, storage_state and the plugin
system are later M4 work and are intentionally not exercised here.)
"""

import pytest

from webclient import RETURN, DOMUpdateEvent, LiveDocument, WebClient

APP = """
<html><head><title>App</title></head><body>
  <div class="card" id="c1"><h2>Card One</h2>
    <button onclick="this.parentElement.insertAdjacentHTML('beforeend',
      '<div class=added>added-one</div>')">grow</button></div>
  <div class="card" id="c2"><h2>Card Two</h2></div>
  <form><input id="name" type="text"><span id="out"></span></form>
  <script>
    console.log("booted");
    document.querySelector("#name").addEventListener("input",
      e => document.querySelector("#out").textContent = e.target.value);
  </script>
</body></html>
"""


@pytest.fixture(scope="module")
def wc():
    with WebClient(timeout=10.0) as client:
        yield client


@pytest.fixture
def app(httpserver, wc):
    httpserver.expect_request("/app").respond_with_data(APP, content_type="text/html")
    live = wc.ref(httpserver.url_for("/app")).resolve(browser=True).collect()
    yield live
    wc.release(live)


def test_browser_fetch_returns_live_document(app):
    assert isinstance(app, LiveDocument)
    assert app.ok and app.kind == "html"
    assert app.select("h2").text_content == "Card One"
    assert app.select('//div[@id="c2"]/h2').text_content == "Card Two"  # xpath


def test_browser_render_emits_a_navigation_event(app):
    # parity with the static path: a browser render records a NavigationEvent, so
    # doc.events is populated even for a page that issues no XHR/console output.
    from webclient import NavigationEvent

    navs = app.events_of(NavigationEvent)
    assert len(navs) == 1
    assert navs[0].status_code == 200 and navs[0].source == "core-browser"
    assert navs[0].document_id == app.id


def test_click_mutates_dom_and_records_everything(app):
    app.click("#c1 button")
    app.wait_for(".added", timeout=5.0)
    assert app.select(".added").text_content == "added-one"
    assert [a.action for a in app.action_events if a.action == "click"] == ["click"]
    assert any(isinstance(e, DOMUpdateEvent) for e in app.dom_mutations)


def test_write_and_live_state(app):
    app.write("#name", "Ada")
    assert app.select("#out").text_content == "Ada"
    assert app.evaluate("document.querySelector('#name').value") == "Ada"


def test_console_capture(app):
    app.wait_for(timeout=0.3)  # let the boot script finish
    assert any("booted" in e.text for e in app.console)


def test_livenode_event_narrowing(app):
    app.click("#c1 button")
    app.wait_for(".added", timeout=5.0)
    card1 = app.select("#c1")
    card2 = app.select("#c2")
    assert len(card1.events_of(DOMUpdateEvent)) > 0
    assert len(card2.events_of(DOMUpdateEvent)) == 0  # sibling untouched


def test_missing_targets_are_loud_unless_policy_returns(app):
    with pytest.raises(LookupError):
        app.click(".nope", timeout=0.3)
    app.click(".nope", timeout=0.3, optional=True)  # optional: no raise
    with pytest.raises(LookupError):
        app.select(".nope")
    assert app.select(".nope", error=RETURN).ok is False  # loud unless error=


def test_screenshot(app):
    shot = app.screenshot()
    assert shot.kind == "binary"
    assert shot.content[:8] == b"\x89PNG\r\n\x1a\n"
    element_shot = app.screenshot("#c1")
    assert element_shot.content[:4] == b"\x89PNG"


def test_reload_reproduces_state(httpserver, wc):
    httpserver.expect_request("/app2").respond_with_data(APP, content_type="text/html")
    live = wc.ref(httpserver.url_for("/app2")).resolve(browser=True).collect()
    live.click("#c1 button").write("#name", "Bob")
    assert [a["op"] for a in live.ref().actions] == ["click", "write"]  # chain recorded
    wc.release(live)

    fresh = live.reload()  # re-resolves + replays the action chain
    try:
        assert fresh.select(".added", error=RETURN).ok
        assert fresh.select("#out").text_content == "Bob"
    finally:
        wc.release(fresh)


INJECTED = """
<html><head><title>Injected</title></head><body>
  <div id="app"></div>
  <script>
    document.getElementById("app").innerHTML =
      "<p>" + Array(80).fill("injected content word").join(" ") + "</p>";
  </script>
</body></html>
"""
PLAIN = (
    "<html><body><main>"
    + "real static content here " * 80
    + "</main></body></html>"
)


def test_probe_mode_flags_js_injected_content(httpserver, wc):
    """``browser="probe"`` resolves both tiers and compares: a page whose content
    is injected by JS is flagged was_browser_required with a positive render_gain,
    and the returned document is the fuller (browser-rendered) one."""
    httpserver.expect_request("/inj").respond_with_data(INJECTED, content_type="text/html")
    doc = wc.fetch(httpserver.url_for("/inj"), browser="probe")
    try:
        assert "injected content word" in doc.text_content  # browser recovered it
        p = doc._probe
        assert p is not None and p.was_browser_required is True
        assert p.js_required is True and p.render_gain and p.render_gain > 0
        assert p.reason == "js_injected_content" and p.escalation == ["static", "browser"]
        facet = doc.summary().probe
        assert facet is not None and facet.was_browser_required and facet.render_gain > 0
    finally:
        wc.release(doc)


def test_probe_mode_reports_static_is_sufficient(httpserver, wc):
    """A page whose content is already in the static HTML: probe returns it with
    was_browser_required False and render_gain 0 -- 'you don't need a browser'."""
    httpserver.expect_request("/plain").respond_with_data(PLAIN, content_type="text/html")
    doc = wc.fetch(httpserver.url_for("/plain"), browser="probe")
    try:
        p = doc._probe
        assert p is not None and p.was_browser_required is False
        assert p.render_gain == 0 and p.reason == "static_sufficient"
    finally:
        wc.release(doc)


def test_release_returns_page_to_pool(httpserver, wc):
    httpserver.expect_request("/p").respond_with_data(
        "<html><body>p</body></html>", content_type="text/html"
    )
    live = wc.ref(httpserver.url_for("/p")).resolve(browser=True).collect()
    before = wc.pool.stats().pages_free
    wc.release(live)
    assert wc.pool.stats().pages_free == before + 1  # a page freed up
    with pytest.raises(Exception):  # its page is gone
        live.click("body", timeout=0.3)


def test_injected_scripts_run_on_live_pages(httpserver):
    """The client injects scripts (``inject_script``) and a backing declares them
    (``Backing.page_scripts``); both are installed on every live page before
    navigation. A fresh client so the module-scoped one is not polluted."""
    from webclient import Backing
    from webclient.clients import PageScript

    class Marker(Backing):  # a backing that instruments live pages
        page_scripts = (PageScript("window.__wc_marker = 'm';", "init"),)

    httpserver.expect_request("/p").respond_with_data(
        "<html><body>x</body></html>", content_type="text/html"
    )
    with WebClient(timeout=10.0) as c:
        c.inject_script("window.__wc_injected = 42;")  # client-level, init phase
        c.use(Marker())  # backing-level
        doc = c.ref(httpserver.url_for("/p")).resolve(browser=True)
        try:
            assert doc.evaluate("() => window.__wc_injected") == 42
            assert doc.evaluate("() => window.__wc_marker") == "m"
        finally:
            c.release(doc)
