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
