"""Facet ops: transport / metadata / structure / signals -- deterministic
projections of a resolved Document (keys-not-values), each a first-class Document
op (there is no aggregating summary()). The ``signals`` facet reports self-describing
:class:`Signal`\\ s (spa / anti_bot / blocked / ...) built from the response."""

import pytest

from webclient import WebClient
from webclient.core.document.models import Metadata, Signal, Structure, Transport

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


def test_spa_signal_reads_captured_browser_events():
    # the spa signal reads DOM/network events a browser render captured -- no browser
    # needed for the projection itself, so we seed the events directly.
    from webclient.core.document import Document
    from webclient.core.document.live import network_event

    doc = Document(
        kind="html",
        url="https://app.example/",
        content=b'<html><script src="/_next/app.js"></script><div id="root"></div></html>',
        status_code=200,
    )
    doc._events = [
        network_event("GET", "https://app.example/api/items", "xhr", doc),
        network_event("POST", "https://app.example/api/track", "fetch", doc),
    ]
    spa = doc.spa()
    assert isinstance(spa, Signal) and spa.present  # a "next" framework marker
    assert spa.remedy == "browser"  # a render would recover the client-built content
    assert doc.framework() == "next"
    eps = doc.xhr_endpoints()  # the SPA's data sources (an agent can hit them direct)
    assert {c.method for c in eps} == {"GET", "POST"}
    assert any("api/items" in c.url for c in eps)


def _load_mut(*, in_main=True):
    # a load-phase "added" mutation (position/count only; the ratio is measured from
    # net text growth via _render_stats, not from mutations -- so reorganising DOM
    # doesn't inflate it).
    from webclient.events import DOMUpdateEvent

    return DOMUpdateEvent(
        kind="added", detail={"ids": [], "phase": "load", "added": 1, "inMain": in_main}
    )


def test_body_injected_signal_graded_by_net_text_not_reorg():
    # the body_injected signal carries the injected ratio; same-origin XHRs ALONE
    # don't flag it, and merely re-organising the DOM (net text unchanged) doesn't.
    from webclient.core.document import Document
    from webclient.core.document.live import network_event

    # (a) XHRs + DOM churn but the text was already there at DCL -> NOT injected
    ssr = Document(kind="html", url="https://news.acme.com/",
                   content=b"<html><body>x</body></html>", status_code=200)
    ssr._render_stats = {"text": 1000, "dclText": 980}  # only 2% net-new after load
    ssr._events = [
        network_event("GET", "https://news.acme.com/a.json", "fetch", ssr),
        network_event("GET", "https://news.acme.com/b.json", "fetch", ssr),
        _load_mut(),
    ]
    bi = ssr.body_injected()
    assert not bi.present and bi.value < 0.4  # value carries the injected ratio
    assert not ssr.spa()  # no SPA-family detector fired

    # (b) most of the text built after DCL -> body_injected fires -> spa rolls it up
    doc = Document(kind="html", url="https://app.acme.com/",
                   content=b"<html><body></body></html>", status_code=200)
    doc._render_stats = {"text": 1000, "dclText": 100}  # 90% net-new after load
    doc._events = [_load_mut()]
    bi = doc.body_injected()
    assert bi.present and bi.value >= 0.4 and bi.remedy == "browser"
    spa = doc.spa()
    assert spa.present and "body_injected" in spa.value  # the roll-up names its evidence


def test_xhr_composed_is_a_distinct_signal_for_the_same_conclusion():
    # the actionable case: content injected INTO THE MAIN AREA from the page's own
    # origin -> a separate signal from body_injected, both may fire for one SPA.
    from webclient.core.document import Document
    from webclient.core.document.live import network_event

    doc = Document(kind="html", url="https://news.acme.com/",
                   content=b"<html><body></body></html>", status_code=200)
    doc._render_stats = {"text": 1000, "dclText": 400}  # 60% net-new after load
    doc._events = [
        network_event("GET", "https://news.acme.com/blocks/hero.plain.html", "fetch", doc),
        _load_mut(in_main=True),
    ]
    xc = doc.xhr_composed()
    assert xc.present and "XHR" in xc.reason
    assert any("hero.plain.html" in u for u in xc.value)  # the data endpoints
    # both xhr_composed AND body_injected fire -> two signals, same conclusion
    names = {s.name for s in doc.signals()}
    assert {"xhr_composed", "body_injected"} <= names

    # same injection but only THIRD-party data -> not a same-origin composition
    doc2 = Document(kind="html", url="https://blog.acme.com/",
                    content=b"<html><body></body></html>", status_code=200)
    doc2._render_stats = {"text": 1000, "dclText": 850}  # 15% net-new, third-party only
    doc2._events = [
        network_event("GET", "https://cdn.ads.example/w", "fetch", doc2),
        _load_mut(in_main=True),
    ]
    assert not doc2.xhr_composed().present


def test_third_party_only_xhr_does_not_flag_a_static_page_as_spa():
    # a server-rendered page whose only XHR/fetch calls are third-party analytics
    # is NOT a SPA (cross-origin beacons don't imply client composition).
    from webclient.core.document import Document
    from webclient.core.document.live import network_event

    doc = Document(
        kind="html",
        url="https://blog.acme.com/post",
        content=b"<html><body><article>Full server-rendered content here.</article></body></html>",
        status_code=200,
    )
    doc._events = [
        network_event("POST", "https://api.segment.io/v1/t", "fetch", doc),
        network_event("GET", "https://www.google-analytics.com/g/collect", "xhr", doc),
    ]
    assert not doc.spa().present  # only cross-origin analytics -> server-rendered


def test_signals_is_a_total_facet_empty_on_a_normal_page(page):
    # signals always applies; an ordinary page reports nothing (no dance, no error).
    assert page.signals() == []
    assert not page.spa() and not page.anti_bot() and not page.blocked()


def test_metadata_and_structure_absent_on_json_but_signals_total(httpserver):
    httpserver.expect_request("/j").respond_with_json({"a": 1})
    with WebClient() as wc:
        doc = wc.fetch(httpserver.url_for("/j")).collect()
        assert doc.transport().kind == "json"  # transport applies to any kind
        assert doc.has_op("signals")  # signals is total -- applies to any kind
        # metadata/structure gate on an html/xml tree
        assert not doc.has_op("metadata")
        assert not doc.has_op("structure")
