"""M4 browser tests (MVP subset). One WebClient (one chromium) for the module.

Covers the implemented live surface: live fetch, interaction (click/write/
wait_for), live selection (css + xpath), console + DOM-mutation capture,
per-element event narrowing, missing-target policy, screenshot, reload replay,
and page release. (xhr capture, DOM snapshots, storage_state and the plugin
system are later M4 work and are intentionally not exercised here.)
"""

import pytest

from webclient import (
    RETURN,
    DOMUpdateEvent,
    LiveDocument,
    WaitConfig,
    WaitEvent,
    WebClient,
)

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


def test_auto_escalates_js_injected_content(httpserver, wc):
    """``browser="auto"`` escalates a JS-gated page (an empty shell whose content is
    injected by script) to a browser render; the returned document is the fuller
    (browser-rendered) one, and its transport trail shows the escalation."""
    httpserver.expect_request("/inj").respond_with_data(INJECTED, content_type="text/html")
    # the static empty shell is what the spa flag fires on (driving the escalation)
    assert wc.fetch(httpserver.url_for("/inj")).spa().present
    doc = wc.fetch(httpserver.url_for("/inj"), browser="auto")
    assert "injected content word" in doc.text_content  # browser recovered it
    assert doc.transport().escalation == ["static", "browser"]
    assert doc.transport().final_tier == "browser"


def test_auto_stays_static_for_a_sparse_page(httpserver, wc):
    # a genuinely sparse static page the browser would not enrich (empty but no
    # bundle to run) is NOT escalated -- it stays on the static tier.
    sparse = "<html><body><main><p>Short login screen.</p></main></body></html>"
    httpserver.expect_request("/sparse").respond_with_data(sparse, content_type="text/html")
    doc = wc.fetch(httpserver.url_for("/sparse"), browser="auto")
    assert doc._page is None  # never launched a browser
    assert doc.transport().final_tier == "static"


def test_auto_escalation_does_not_leak_the_page(httpserver, wc):
    # a content-only browser escalation returns its page to the pool automatically
    # -- no explicit release needed, no lease leak, and the content survives.
    httpserver.expect_request("/inj").respond_with_data(INJECTED, content_type="text/html")
    before = wc.pool.stats().pages_free
    doc = wc.fetch(httpserver.url_for("/inj"), browser="auto")
    assert wc.pool.stats().pages_free == before  # page already returned
    assert "injected content word" in doc.text_content  # content survives release


def test_auto_returns_static_when_content_is_already_present(httpserver, wc):
    """A page whose content is already in the static HTML is returned as-is on the
    static tier -- 'you don't need a browser'."""
    httpserver.expect_request("/plain").respond_with_data(PLAIN, content_type="text/html")
    doc = wc.fetch(httpserver.url_for("/plain"), browser="auto")
    assert doc._page is None and doc.transport().final_tier == "static"
    assert "real static content" in doc.text_content


def test_crawl_browser_captures_xhr_endpoints_into_frontier(httpserver, wc):
    # a browser crawl observes the page's data-API (fetch/XHR) calls and adds them
    # to the frontier so the crawl covers them too.
    page = (
        "<html><body><h1>App</h1>"
        "<script>fetch('/api/items').then(r => r.json());</script>"
        "</body></html>"
    )
    httpserver.expect_request("/app").respond_with_data(page, content_type="text/html")
    httpserver.expect_request("/api/items").respond_with_json({"items": [1, 2, 3]})
    with wc.crawl(
        httpserver.url_for("/app"),
        auto=True,
        browser=True,
        max_pages=1,  # fetch only /app, then inspect the frontier
        depth=2,
        obey_robots=False,
    ) as crawl:
        crawl.step()
    assert any("/api/items" in e.url and e.text == "[xhr]" for e in crawl.frontier)


def test_skeleton_marks_xhr_injected_content_on_a_real_spa(httpserver, wc):
    # a JS page that issues a fetch and injects a list: browser="auto" renders it,
    # and skeleton() marks the injected nodes [xhr] and lists the data API, while
    # the server-initial shell stays unmarked. (Feature A on a real SPA.)
    page = (
        "<html><body><div id='app'></div>"
        "<script>fetch('/api/items');"
        "document.getElementById('app').innerHTML ="
        "  '<ul class=\"list\">' + '<li class=\"item\">x</li>'.repeat(3) + '</ul>';"
        "</script></body></html>"
    )
    httpserver.expect_request("/spa").respond_with_data(page, content_type="text/html")
    httpserver.expect_request("/api/items").respond_with_json({"items": [1, 2, 3]})
    doc = wc.fetch(httpserver.url_for("/spa"), browser="auto")  # escalates: empty shell
    sk = doc.skeleton()
    assert "/api/items" in sk                       # observed data API listed
    assert '<div id="app">' in sk                    # the shell node: server-initial
    item_line = next(l for l in sk.splitlines() if '<li class="item">' in l)
    assert "[xhr]" in item_line                     # injected -> marked xhr
    assert sk.count('<li class="item">') == 3       # all 3 shown faithfully (no collapse)
    # collapse=True merges the identical injected items
    assert '<li class="item"> [xhr] ×3' in doc.skeleton(collapse=True)


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


def test_execute_plan_with_browser_always_selects_on_engine_loop(httpserver, wc):
    # Regression: a plan run by wc.execute resolves on the ENGINE loop, so a live
    # doc's select/select_all (which normally sync-bridge to the page) must not try
    # to bridge from the engine loop thread -- they fall back to an in-memory select
    # on the captured rendered content. (Was: RuntimeError "sync facade method
    # called from the engine loop thread".)
    from webclient import wq

    page = (
        '<html><body><h1 id="h">Hi</h1>'
        '<div class="card"><span class="t">A</span></div>'
        '<div class="card"><span class="t">B</span></div></body></html>'
    )
    httpserver.expect_request("/e").respond_with_data(page, content_type="text/html")
    url = httpserver.url_for("/e")

    single = wq.ref.resolve(browser="always").select("#h").text_content
    assert wc.execute(single, wc.ref(url)).get() == "Hi"

    rows = (
        wq.ref.resolve(browser="always")
        .select_all(".card")
        .extract(t=wq.doc.select(".t").text_content)
        .project()
    )
    assert wc.execute(rows, wc.ref(url)) == [{"t": "A"}, {"t": "B"}]


def test_plan_auto_releases_browser_pages(httpserver, wc):
    # a plan has no release(doc) handle, so wc.execute must return the browser
    # pages it resolved to the pool when it finishes (no lease leak).
    from webclient import wq

    httpserver.expect_request("/r").respond_with_data(
        '<html><body><h1 id="h">Hi</h1></body></html>', content_type="text/html"
    )
    url = httpserver.url_for("/r")
    before = wc.pool._held.get("page", 0)
    for _ in range(3):
        wc.execute(wq.ref.resolve(browser="always").select("#h").text_content, wc.ref(url))
    assert wc.pool._held.get("page", 0) == before  # every plan page released


def test_keep_alive_page_is_caller_owned(httpserver, wc):
    # keep_alive marks the page caller-owned: it survives (a plan wouldn't release
    # it) until the caller releases it. A numeric keep_alive adds a TTL safety net.
    import time

    httpserver.expect_request("/k").respond_with_data(
        "<html><body>x</body></html>", content_type="text/html"
    )
    url = httpserver.url_for("/k")

    doc = wc.fetch(url, browser=True, keep_alive=True)
    assert doc._page is not None and doc._keep_alive is True
    wc.release(doc)
    assert doc._page is None

    ttl_doc = wc.fetch(url, browser=True, keep_alive=0.5)
    assert ttl_doc._page is not None
    time.sleep(1.0)
    assert ttl_doc._page is None  # TTL released it


def test_plan_interact_then_select_sees_post_interaction_dom(httpserver, wc):
    # a plan runs on the engine loop, so select takes the in-memory fallback; drain
    # refreshes the doc's content after each interaction, so the fallback sees the
    # post-click DOM (regression: it used to select the ORIGINAL render -> 0 matches).
    from webclient import wq

    app = (
        '<html><body><button id="b" onclick="document.body.insertAdjacentHTML'
        "('beforeend','<div class=added>NEW</div>')\">go</button></body></html>"
    )
    httpserver.expect_request("/i").respond_with_data(app, content_type="text/html")
    rows = wc.execute(
        wq.ref.resolve(browser="always").click("#b").select_all(".added").project(),
        wc.ref(httpserver.url_for("/i")),
    )
    assert len(rows) == 1  # the click's node is visible to the subsequent select


# -- Ask 1: real Playwright response info (status / headers / final URL) --------

def test_browser_captures_real_status_and_headers(httpserver, wc):
    """A browser render now carries the REAL main-navigation status + response
    headers Playwright reported -- not a fabricated 200 / empty headers -- so
    ``doc.transport()`` is accurate on a browser-rendered page (Ask 1)."""
    httpserver.expect_request("/nf").respond_with_data(
        "<html><body>nope</body></html>",
        status=404,
        content_type="text/html",
        headers={"X-Custom-Thing": "yes"},
    )
    doc = wc.fetch(httpserver.url_for("/nf"), browser=True)
    try:
        assert doc.status_code == 404  # the true status, not 200
        assert doc.ok is False  # non-2xx -> not ok, error populated (http parity)
        t = doc.transport()
        assert t.status_code == 404
        assert "x-custom-thing" in t.header_keys  # real response headers threaded
        assert t.final_tier == "browser"
        assert doc.final_url.endswith("/nf")
    finally:
        wc.release(doc)


def test_browser_fetch_of_blocked_page_fires_flags(httpserver, wc):
    """Because the real status + headers now reach the browser Document, the ``flags``
    access facet (which reads status/headers) fires on a browser fetch of a blocked
    page -- impossible when the status was hard-coded 200 (Ask 1)."""
    httpserver.expect_request("/forbidden").respond_with_data(
        "<html><body>Forbidden</body></html>", status=403, content_type="text/html"
    )
    doc = wc.fetch(httpserver.url_for("/forbidden"), browser=True)
    try:
        assert doc.status_code == 403
        assert doc.transport().status_code == 403
        assert doc.anti_bot_triggered().present  # a bare-status 403 challenge
        assert doc.anti_bot_triggered().remedy == "proxy"
    finally:
        wc.release(doc)


# -- Ask 2: controllable wait-for-stable-DOM (enum + timeout + policy) ----------

DELAYED = """
<html><body><div id="base">base</div>
<script>
  setTimeout(function () {
    var d = document.createElement('div');
    d.id = 'late'; d.textContent = 'late-content';
    document.body.appendChild(d);
  }, 1500);
</script></body></html>
"""


def test_wait_selector_reaches_the_dom_state(httpserver, wc):
    # WaitEvent.SELECTOR waits precisely until the (delayed) element appears.
    httpserver.expect_request("/w1").respond_with_data(DELAYED, content_type="text/html")
    doc = wc.fetch(
        httpserver.url_for("/w1"),
        browser=True,
        wait=WaitConfig(event=WaitEvent.SELECTOR, selector="#late", timeout=5.0),
    )
    try:
        assert doc.select("#late", error=RETURN).ok
        assert "late-content" in doc.text_content
    finally:
        wc.release(doc)


def test_wait_domcontentloaded_snapshots_early(httpserver, wc):
    # WaitEvent.DOMCONTENTLOADED returns at parse time -- the delayed element is
    # not in the snapshot yet, showing the mode reaches its own (earlier) state.
    httpserver.expect_request("/w2").respond_with_data(DELAYED, content_type="text/html")
    doc = wc.fetch(
        httpserver.url_for("/w2"),
        browser=True,
        wait=WaitConfig(event=WaitEvent.DOMCONTENTLOADED, timeout=5.0),
    )
    try:
        assert "base" in doc.text_content  # the served DOM is there
        assert doc.select("#late", error=RETURN).ok is False  # not injected yet
    finally:
        wc.release(doc)


def test_wait_timeout_raises_by_default_and_returns_partial(httpserver, wc):
    # loud-by-default: a wait whose milestone never arrives raises; under
    # on_timeout=RETURN it hands back the partial DOM instead (Ask 2 timeout policy).
    httpserver.expect_request("/w3").respond_with_data(DELAYED, content_type="text/html")
    url = httpserver.url_for("/w3")

    with pytest.raises(LookupError):  # RAISE is the default on_timeout
        wc.fetch(
            url,
            browser=True,
            wait=WaitConfig(event=WaitEvent.SELECTOR, selector="#never", timeout=0.5),
        )

    doc = wc.fetch(
        url,
        browser=True,
        wait=WaitConfig(
            event=WaitEvent.SELECTOR, selector="#never", timeout=0.5, on_timeout=RETURN
        ),
    )
    try:
        assert doc.ok  # returned what rendered so far
        assert "base" in doc.text_content
        assert doc.select("#never", error=RETURN).ok is False
    finally:
        wc.release(doc)


def test_browser_policy_wait_for_is_honoured(httpserver, wc):
    # the existing BrowserPolicy.wait_for hook now drives a SELECTOR wait through
    # the browser= path (no explicit WaitConfig needed).
    from webclient import BrowserPolicy

    httpserver.expect_request("/w4").respond_with_data(DELAYED, content_type="text/html")
    doc = wc.fetch(
        httpserver.url_for("/w4"),
        browser=BrowserPolicy(when="always", wait_for="#late"),
    )
    try:
        assert doc.select("#late", error=RETURN).ok
    finally:
        wc.release(doc)


# -- Ask 3: DOM x network correlation quality (browser feeds the signals) -------

XHR_SPA = """
<html><body><main id="app"></main>
<script>
  fetch('/api/data').then(function (r) { return r.json(); }).then(function (d) {
    document.getElementById('app').innerHTML =
      '<ul class="list">' +
      d.rows.map(function (x) { return '<li>' + x + '</li>'; }).join('') +
      '</ul>';
  });
</script></body></html>
"""


def test_browser_feeds_dom_x_network_correlation_signals(httpserver, wc):
    """A real SPA fetch: the browser driver captures the DOM-mutation phase / inMain
    / added detail AND the network resource_type the ``flags`` facet correlates,
    so the spa flag's xhr_composed / body_injected signals fire (Ask 3)."""
    from webclient.models import NetworkEvent

    httpserver.expect_request("/xhrspa").respond_with_data(
        XHR_SPA, content_type="text/html"
    )
    httpserver.expect_request("/api/data").respond_with_json(
        {"rows": ["a long injected row of content words " * 3,
                  "another long injected row of content words " * 3]}
    )
    doc = wc.fetch(httpserver.url_for("/xhrspa"), browser=True)
    try:
        muts = [e for e in doc.events if isinstance(e, DOMUpdateEvent)]
        load_added = [
            e for e in muts
            if e.detail.get("phase") == "load" and e.kind == "added"
        ]
        assert load_added, "no load-phase 'added' DOM mutation captured"
        assert any(e.detail.get("inMain") for e in load_added)  # ancestor detail
        assert all("added" in e.detail for e in load_added)  # size detail

        xhr = [
            e for e in doc.events
            if isinstance(e, NetworkEvent) and e.resource_type in ("xhr", "fetch")
        ]
        assert any(
            "/api/data" in (n.request.dispatch("url") if n.request else "")
            for n in xhr
        )  # resource_type captured as fetch/xhr

        # the correlation the driver's data feeds -> both signals inside the spa flag:
        spa = doc.spa()
        names = {s.name for s in spa.signals}
        assert spa.present and {"xhr_composed", "body_injected"} <= names
        assert any("/api/data" in c.url for c in doc.xhr_endpoints())
        assert any("/api/data" in u for u in (spa.value or []))
    finally:
        wc.release(doc)
