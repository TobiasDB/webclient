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


def test_summary_includes_arbitrary_backing_methods_as_extra(page):
    # a name that is not a facet but is a backing op (``title``) is called and
    # placed under ``extra`` -- the open mechanism a crawl uses to pick backings.
    s = page.summary("transport", "title")
    assert s.transport and s.metadata is None  # only the named facet
    assert s.extra == {"title": "Widgets"}
    # default (no include) carries no extra section.
    assert page.summary().extra == {}


def test_summary_rejects_an_unknown_facet_name(page):
    # a typo is an error, not a silently-dropped section (E-M3).
    import pytest

    with pytest.raises(LookupError, match="structrue"):
        page.summary("transport", "structrue")


def test_runtime_facet_reads_captured_browser_events():
    # runtime reads DOM/network events a browser render captured -- no browser
    # needed for the projection itself, so we seed the events directly.
    from webclient.core.document import Document
    from webclient.core.document.live import network_event
    from webclient.events import DOMUpdateEvent
    from webclient.summary import Runtime

    doc = Document(
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


def _load_mut(*, in_main=True):
    # a load-phase "added" mutation (position/count only; the ratio is measured from
    # net text growth via _render_stats, not from mutations -- so reorganising DOM
    # doesn't inflate it).
    from webclient.events import DOMUpdateEvent

    return DOMUpdateEvent(
        kind="added", detail={"ids": [], "phase": "load", "added": 1, "inMain": in_main}
    )


def test_spa_graded_by_net_injected_text_not_raw_xhr_or_reorg():
    # the new balance: same-origin XHRs ALONE no longer flag a SPA (too greedy), and
    # merely re-organising the DOM (net text unchanged) is not client rendering.
    from webclient.core.document import Document
    from webclient.core.document.live import network_event

    # (a) XHRs + DOM churn but the text was already there at DCL -> NOT a SPA
    ssr = Document(kind="html", url="https://news.acme.com/",
                   content=b"<html><body>x</body></html>", status_code=200)
    ssr._render_stats = {"text": 1000, "dclText": 980}  # only 2% net-new after load
    ssr._events = [
        network_event("GET", "https://news.acme.com/a.json", "fetch", ssr),
        network_event("GET", "https://news.acme.com/b.json", "fetch", ssr),
        _load_mut(),
    ]
    r = ssr.dispatch("runtime")
    assert r.injected_ratio < 0.4 and r.is_spa is False

    # (b) most of the text built after DCL -> a SPA (no framework marker needed)
    spa = Document(kind="html", url="https://app.acme.com/",
                   content=b"<html><body></body></html>", status_code=200)
    spa._render_stats = {"text": 1000, "dclText": 100}  # 90% net-new after load
    spa._events = [_load_mut()]
    r = spa.dispatch("runtime")
    assert r.injected_ratio >= 0.4 and r.is_spa is True and r.injected_nodes == 1


def test_content_from_xhr_needs_main_injection_plus_own_origin_data():
    # the actionable signal: substantial content injected INTO THE MAIN AREA from the
    # page's own origin -> an agent can skip the render and hit the endpoints.
    from webclient.core.document import Document
    from webclient.core.document.live import network_event

    doc = Document(kind="html", url="https://news.acme.com/",
                   content=b"<html><body></body></html>", status_code=200)
    doc._render_stats = {"text": 1000, "dclText": 400}  # 60% net-new after load
    doc._events = [
        network_event("GET", "https://news.acme.com/blocks/hero.plain.html", "fetch", doc),
        _load_mut(in_main=True),
    ]
    r = doc.dispatch("runtime")
    assert r.content_from_xhr is True and r.injected_in_main is True and r.is_spa is True
    assert "content from XHR" in str(doc.dispatch("summary"))

    # same injection but only THIRD-party data -> not content_from_xhr
    doc2 = Document(kind="html", url="https://blog.acme.com/",
                    content=b"<html><body></body></html>", status_code=200)
    doc2._render_stats = {"text": 1000, "dclText": 400}
    doc2._events = [
        network_event("GET", "https://cdn.ads.example/w", "fetch", doc2),
        _load_mut(in_main=True),
    ]
    assert doc2.dispatch("runtime").content_from_xhr is False


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
    r = doc.dispatch("runtime")
    assert r.is_spa is False  # only cross-origin analytics -> still server-rendered


def test_metadata_and_structure_absent_on_json(httpserver):
    httpserver.expect_request("/j").respond_with_json({"a": 1})
    with WebClient() as wc:
        doc = wc.fetch(httpserver.url_for("/j")).collect()
        assert doc.transport().kind == "json"  # transport applies to any kind
        # metadata/structure gate on an html/xml tree
        assert not doc.has_op("metadata")
        assert not doc.has_op("structure")


def test_summary_prints_readable_llm_text(page):
    # print(summary) yields a compact digest of the present facets, not a pydantic
    # repr -- the form an LLM reads directly.
    text = str(page.summary())
    assert "[200 ok]" in text
    assert "title: Widgets" in text
    assert "description: The finest widgets." in text
    assert "lang=en" in text
    assert "headings: Widgets > Blue > Red" in text
    assert "form(s)" in text and "post" in text
    # an empty summary is labelled, not blank.
    from webclient.summary import Summary

    assert str(Summary()) == "(empty summary)"


def test_summary_skeleton_field_is_opt_in(page):
    # skeleton is a typed field, populated only when requested by name (kept out of
    # the default lean summary).
    assert page.summary().skeleton is None
    s = page.summary("skeleton")
    assert s.skeleton is not None and "form" in s.skeleton
