"""R3: the browser backing. The gate is that the same expressions produce the
same results over a page as over a tree."""
import pytest

from webclient import (StaleDocument, UnsupportedOperation, doc, el, field)

pytest.importorskip("playwright")

LIVE = """<html><head><title>Live</title></head><body>
<h1>Dashboard</h1>
<table id="rows">
  <tr class="row"><td class="name">alpha</td><td class="size">1</td></tr>
  <tr class="row"><td class="name">beta</td><td class="size">2</td></tr>
</table>
<button id="more">more</button>
<a id="next" href="/second">next</a>
<script>
document.getElementById('more').addEventListener('click', () => {
  setTimeout(() => {
    const tr = document.createElement('tr');
    tr.className = 'row';
    tr.innerHTML = '<td class="name">gamma</td><td class="size">3</td>';
    document.getElementById('rows').appendChild(tr);
  }, 120);
});
</script>
</body></html>"""

SECOND = """<html><head><title>Second</title></head><body>
<h1>Second</h1></body></html>"""


@pytest.fixture
def live_site(httpserver):
    httpserver.expect_request("/live").respond_with_data(
        LIVE, content_type="text/html")
    httpserver.expect_request("/second").respond_with_data(
        SECOND, content_type="text/html")
    return httpserver


@pytest.fixture
def browser_doc(live_site, wc):
    document = wc.resolve(live_site.url_for("/live"), browser=True)
    yield document
    try:
        wc.release(document)
    except Exception:
        pass


def test_a_page_backing_supports_browser_ops(browser_doc):
    assert browser_doc.supports("browser") is True
    assert browser_doc.attr("title").get() == "Live"
    assert browser_doc.select("h1").attr("text").get() == "Dashboard"


def test_the_same_expression_over_both_backings(live_site, wc):
    expression = doc.select_all(".row").map(
        el.select(".name").attr("text").alias("name"),
        size=el.select(".size").attr("text"),
    )
    static = wc.resolve(live_site.url_for("/live"))
    live = wc.resolve(live_site.url_for("/live"), browser=True)
    try:
        assert ([dict(r) for r in expression.collect(live)]
                == [dict(r) for r in expression.collect(static)])
    finally:
        wc.release(live)


def test_interaction_then_wait_stable_sees_new_rows(browser_doc):
    assert len(browser_doc.select_all(".row")) == 2
    browser_doc.click("#more").wait_stable(quiet_ms=200)
    assert len(browser_doc.select_all(".row")) == 3


def test_links_resolve_and_elements_address_stably(browser_doc):
    element = browser_doc.select(".row", index=1)
    assert element.path.startswith("nid:")
    assert element.select(".name").attr("text").get() == "beta"
    # the address survives a mutation elsewhere in the page
    browser_doc.click("#more").wait_stable(quiet_ms=200)
    assert element.select(".name").attr("text").get() == "beta"


def test_navigate_leaves_the_old_document_readable(live_site, wc, browser_doc):
    second = browser_doc.navigate("/second")
    try:
        assert second.select("h1").attr("text").get() == "Second"
        # the old document keeps its static half
        assert browser_doc.render("markdown").get().startswith("# Dashboard")
        assert browser_doc.status_code.get() == 200
        assert browser_doc.supports("browser") is False
        with pytest.raises(StaleDocument) as caught:
            browser_doc.click("#more")
        assert "/second" in str(caught.value)
    finally:
        wc.release(second)


def test_release_returns_the_lease(live_site, wc):
    document = wc.resolve(live_site.url_for("/live"), browser=True)
    assert wc.stats().pages_held == 1
    wc.release(document)
    assert wc.stats().pages_held == 0
    assert document.render("markdown").get()          # still readable


def test_screenshot_is_a_binary_document(browser_doc):
    shot = browser_doc.screenshot("#rows")
    assert shot.kind.get() == "binary"
    assert shot.content.get()[:4] == b"\x89PNG"


def test_evaluate(browser_doc):
    assert browser_doc.evaluate("() => document.title").get() == "Live"
