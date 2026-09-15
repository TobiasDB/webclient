"""Content-type handling: sniffing (html/json/xml/binary), the ops each kind
exposes, and a Reference's ``expect`` hint overriding a mislabelled response."""

import pytest

from webclient import WebClient, wq
from webclient.clients.http import sniff_kind


@pytest.fixture
def wc():
    with WebClient() as client:
        yield client


# -- sniff_kind (pure) ---------------------------------------------------------


@pytest.mark.parametrize(
    "ctype, body, expected",
    [
        ("text/html; charset=utf-8", b"<html></html>", "html"),
        ("application/json", b"{}", "json"),
        ("application/vnd.api+json", b"{}", "json"),   # +json suffix
        ("text/xml", b"<a/>", "xml"),
        ("application/xml", b"<a/>", "xml"),
        ("application/rss+xml", b"<rss/>", "xml"),      # +xml suffix
        ("text/plain", b"hello", "html"),               # text/* parses as html
        ("image/png", b"\x89PNG\r\n", "binary"),
        ("application/octet-stream", b"\x00\x01", "binary"),
    ],
)
def test_sniff_from_content_type(ctype, body, expected):
    assert sniff_kind(ctype, body) == expected


@pytest.mark.parametrize(
    "body, expected",
    [
        (b"  <!DOCTYPE html><html>", "html"),
        (b"<html>", "html"),
        (b'<?xml version="1.0"?><rss/>', "xml"),
        (b'{"a": 1}', "json"),
        (b"[1, 2, 3]", "json"),
        (b"<svg>...</svg>", "html"),   # a bare tag with no CT -> html
        (b"\x89PNG\r\n\x1a\n", "binary"),
        (b"", "binary"),               # empty, unlabelled -> binary
    ],
)
def test_sniff_from_body_when_header_missing(body, expected):
    assert sniff_kind(None, body) == expected


def test_sniff_hint_overrides_a_mislabelled_response():
    # a JSON API that (wrongly) serves text/plain: sniffing would say html.
    assert sniff_kind("text/plain", b'{"n": 1}') == "html"
    assert sniff_kind("text/plain", b'{"n": 1}', hint="json") == "json"
    # a bogus hint is ignored (falls through to real sniffing)
    assert sniff_kind("application/json", b"{}", hint="nonsense") == "json"


# -- live fetch of each kind ---------------------------------------------------


def test_fetch_json_document(httpserver, wc):
    httpserver.expect_request("/api").respond_with_json({"items": [{"n": 1}, {"n": 2}]})
    doc = wc.fetch(httpserver.url_for("/api"))
    assert doc.kind == "json"
    assert doc.select("items[1].n").text_content == "2"
    assert doc.select("items[0].n").attr("value").get() == 1
    els = doc.render("elements")
    assert any(e.id == "items[0].n" and e.text == "1" for e in els)


def test_fetch_xml_document(httpserver, wc):
    rss = (
        b'<?xml version="1.0"?><rss><channel>'
        b"<item><title>First</title></item>"
        b"<item><title>Second</title></item>"
        b"</channel></rss>"
    )
    httpserver.expect_request("/feed").respond_with_data(rss, content_type="application/rss+xml")
    doc = wc.fetch(httpserver.url_for("/feed"))
    assert doc.kind == "xml"
    # xml is served by the tree backing: select + text_content work
    titles = [t.text_content for t in doc.select_all("title")]
    assert titles == ["First", "Second"]


def test_fetch_binary_document(httpserver, wc):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    httpserver.expect_request("/img").respond_with_data(png, content_type="image/png")
    doc = wc.fetch(httpserver.url_for("/img"))
    assert doc.kind == "binary"
    assert doc.content == png
    # a treeless kind has no select op -> a typed error, not a crash
    with pytest.raises(TypeError, match="not available"):
        doc.select("a")


def test_binary_transport_facet_still_works(httpserver, wc):
    # summary's transport facet applies to any kind (metadata/structure do not).
    httpserver.expect_request("/bin").respond_with_data(b"\x00\x01\x02", content_type="application/octet-stream")
    s = wc.fetch(httpserver.url_for("/bin")).summary()
    assert s.transport is not None and s.transport.kind == "binary"
    assert s.metadata is None and s.structure is None


# -- the Reference.expect hint end to end --------------------------------------


def test_expect_hint_forces_json_on_a_mislabelled_endpoint(httpserver, wc):
    # server sends JSON but labels it text/plain; without the hint it sniffs html.
    httpserver.expect_request("/badct").respond_with_data(
        b'{"ok": true, "n": 5}', content_type="text/plain"
    )
    url = httpserver.url_for("/badct")
    assert wc.fetch(url).kind == "html"                        # mislabelled -> wrong
    hinted = wc.fetch(url, expect="json")                      # the hint corrects it
    assert hinted.kind == "json"
    assert hinted.select("n").attr("value").get() == 5


def test_expect_defaults_to_none_and_does_not_misguide(httpserver, wc):
    httpserver.expect_request("/ok").respond_with_json({"n": 1})
    ref = wc.ref(httpserver.url_for("/ok"))
    assert ref.expect is None            # no hint by default
    assert ref.resolve().kind == "json"  # pure sniffing still works


def test_not_operator_evaluates(httpserver, wc):
    # `~expr` records op "not"; regression: it used to KeyError (500) at execution.
    page = b'<html><body><div class="item">a</div>' \
           b'<div class="item"><span class="hide">x</span>b</div></body></html>'
    httpserver.expect_request("/n").respond_with_data(page, content_type="text/html")
    # keep only items with NO .hide child (the ~ predicate must evaluate)
    kept = (
        wc.fetch(httpserver.url_for("/n"))
        .select_all(".item")
        .filter(~wq.doc.select(".hide").is_ok())
        .project()
    )
    assert len(kept) == 1  # the first item (no .hide) survived
