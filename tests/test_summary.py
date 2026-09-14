"""Summary facet backings: transport / metadata / structure -- deterministic
projections of a resolved Document (keys-not-values, no escalation)."""

import pytest

from webclient import WebClient
from webclient.summary import Metadata, Structure, Summary, Transport

PAGE = """
<html lang="en">
<head>
  <title>Widgets</title>
  <meta name="description" content="The finest widgets.">
  <meta property="og:title" content="Widgets"><meta property="og:type" content="website">
  <link rel="canonical" href="/home">
  <link rel="alternate" type="application/rss+xml" href="/feed.xml">
  <script type="application/ld+json">{"@type": "Product", "name": "Widget"}</script>
</head>
<body>
  <main>
    <h1>Widgets</h1><h2>Blue</h2><h2>Red</h2>
    <p>Some words here about widgets and things.</p>
    <a href="/about">about</a><a href="https://ext.example/x">ext</a>
    <a href="#top">skip</a>
    <img src="/a.png"><img src="/b.png">
    <form method="post" action="/buy"><input name="qty"><input name="sku"></form>
    <div class="pagination"><a rel="next" href="/p2">next</a></div>
  </main>
</body></html>
"""


@pytest.fixture
def page(httpserver):
    httpserver.expect_request("/").respond_with_data(
        PAGE, content_type="text/html", headers={"Server": "nginx", "CF-RAY": "1-x"}
    )
    with WebClient() as wc:
        yield wc.fetch(httpserver.url_for("/")).collect()


def test_transport_facet_reports_keys_not_values(page):
    t = page.transport()
    assert isinstance(t, Transport)
    assert t.ok and t.status_code == 200 and t.kind == "html"
    assert t.content_type and "text/html" in t.content_type
    assert t.cdn == "cloudflare"  # from CF-RAY
    assert "server" in t.header_keys and "cf-ray" in t.header_keys  # httpx lowercases
    assert t.size_bytes and t.size_bytes > 0


def test_metadata_facet_extracts_head_and_schema(page):
    m = page.metadata()
    assert isinstance(m, Metadata)
    assert m.title == "Widgets"
    assert m.description == "The finest widgets."
    assert m.lang == "en"
    assert m.canonical_url and m.canonical_url.endswith("/home")
    assert m.schema_types == ["Product"] and m.page_type == "Product"
    assert "og:title" in m.og_keys and "og:type" in m.og_keys
    assert m.feeds and m.feeds[0].endswith("/feed.xml")


def test_structure_facet_maps_body_shape(page):
    s = page.structure()
    assert isinstance(s, Structure)
    assert [(e.level, e.text) for e in s.toc] == [
        (1, "Widgets"),
        (2, "Blue"),
        (2, "Red"),
    ]
    assert s.main_content_present is True
    # internal: /about + /p2 (the rel=next link); external: ext; '#top' excluded
    assert s.links_internal == 2 and s.links_external == 1
    assert s.forms and s.forms[0].method == "post"
    assert s.forms[0].field_names == ["qty", "sku"]
    assert s.pagination == "next-link"
    assert s.media_img == 2
    assert s.word_count and s.reading_time_min == 1


def test_summary_unifier_assembles_and_selects_facets(page):
    full = page.summary()  # default: every applicable facet
    assert isinstance(full, Summary)
    assert full.transport and full.metadata and full.structure
    assert (
        full.runtime is None and full.probe is None
    )  # not applicable to a static fetch
    assert full.metadata.title == "Widgets"

    only = page.summary("transport", "metadata")
    assert only.transport and only.metadata and only.structure is None

    less = page.summary(exclude="structure")
    assert less.transport and less.metadata and less.structure is None


def test_runtime_facet_reads_captured_browser_events():
    # runtime reads DOM/network events a browser render captured -- no browser
    # needed for the projection itself, so we seed the events directly.
    from webclient.core.document import DocumentCore
    from webclient.core.document.live import network_event
    from webclient.events import DOMUpdateEvent
    from webclient.summary import Runtime

    doc = DocumentCore(
        kind="html",
        url="https://app.example/",
        content=b'<html><script src="/_next/app.js"></script><div id="root"></div></html>',
        status_code=200,
    )
    doc._events = [
        network_event("GET", "https://app.example/api/items", "xhr", doc),
        network_event("POST", "https://app.example/api/track", "fetch", doc),
        DOMUpdateEvent(kind="added", selector="#cart"),
    ]
    r = doc.dispatch("runtime")
    assert isinstance(r, Runtime)
    assert r.framework == "next" and r.is_spa is True
    assert r.uses_xhr and r.uses_fetch
    assert {c.method for c in r.xhr_endpoints} == {"GET", "POST"}
    assert any("api/items" in c.url for c in r.xhr_endpoints)
    assert r.dynamic_elements == ["added:#cart"]
    # the unifier now includes the runtime section (it applies -- events captured)
    assert doc.dispatch("summary").runtime is not None


def test_metadata_and_structure_absent_on_json(httpserver):
    httpserver.expect_request("/j").respond_with_json({"a": 1})
    with WebClient() as wc:
        doc = wc.fetch(httpserver.url_for("/j")).collect()
        assert doc.transport().kind == "json"  # transport applies to any kind
        # metadata/structure gate on an html/xml tree
        assert not doc.has_op("metadata")
        assert not doc.has_op("structure")
