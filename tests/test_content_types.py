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


def test_json_attr_missing_key_is_a_miss_not_the_whole_node(wc, httpserver):
    # json attr now honours optional/error and never silently returns the parent
    # node on a missing key (unified with html attr).
    from webclient import WebException

    httpserver.expect_request("/j").respond_with_json({"product": {"name": "Widget"}})
    node = wc.fetch(httpserver.url_for("/j")).select("product")
    assert node.attr("name").get() == "Widget"  # present key
    with pytest.raises(WebException):
        node.attr("price")  # missing key raises (structured), not Field(whole node)
    assert node.attr("price", optional=True).ok is False  # lenient -> not-ok


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


def test_meta_charset_is_honoured_for_non_utf8(httpserver, wc):
    # a windows-1251 page declaring its charset only via <meta> (no HTTP charset):
    # lxml must decode via the meta (bytes handed to the parser), not fall back to
    # latin-1 mojibake. (R-M4)
    cyrillic = "привет".encode("windows-1251")  # non-utf8 bytes
    body = (
        b'<html><head><meta charset="windows-1251"></head>'
        b"<body><p>" + cyrillic + b"</p></body></html>"
    )
    httpserver.expect_request("/cp").respond_with_data(body, content_type="text/html")
    doc = wc.fetch(httpserver.url_for("/cp"))
    assert "привет" in doc.select("p").text_content


def test_html_degrades_on_a_bogus_charset(wc, httpserver):
    # a mislabelled Content-Type charset must not crash html() (re-review F1).
    httpserver.expect_request("/b").respond_with_data(
        b"<html><body><p>hi</p></body></html>",
        content_type="text/html; charset=unknown-8bit",
    )
    doc = wc.fetch(httpserver.url_for("/b"))
    assert "<p>hi</p>" in doc.html()  # no LookupError
    assert doc.markdown() == "hi"


def test_bom_prefixed_single_block_renders(wc, httpserver):
    # a UTF-8 BOM must be stripped so a doctype-less single-block page still renders
    # (re-review F2).
    httpserver.expect_request("/bom").respond_with_data(
        b"\xef\xbb\xbf<html><body><p>Only para</p></body></html>",
        content_type="text/html",
    )
    doc = wc.fetch(httpserver.url_for("/bom"))
    assert doc.markdown() == "Only para"
    assert [e.text for e in doc.elements()] == ["Only para"]


def test_http_charset_header_is_honoured(httpserver, wc):
    # charset declared ONLY in the HTTP Content-Type (no in-document meta): the
    # header is authoritative and must decode correctly (regression guard).
    cyrillic = "привет".encode("windows-1251")
    body = b"<html><body><p>" + cyrillic + b"</p></body></html>"
    httpserver.expect_request("/hc").respond_with_data(
        body, content_type="text/html; charset=windows-1251"
    )
    doc = wc.fetch(httpserver.url_for("/hc"))
    assert "привет" in doc.select("p").text_content


def test_namespaced_xml_is_parsed_as_xml(httpserver, wc):
    # an Atom feed (namespaced XML) is parsed with the XML parser, not the HTML
    # parser -- structure/text preserved, no crash. (R-M3)
    atom = (
        b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
        b"<entry><title>Hello Atom</title></entry></feed>"
    )
    httpserver.expect_request("/atom").respond_with_data(
        atom, content_type="application/atom+xml"
    )
    doc = wc.fetch(httpserver.url_for("/atom"))
    assert doc.kind == "xml"
    assert "Hello Atom" in doc.text_content


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


def test_mislabelled_json_does_not_crash(httpserver, wc):
    # a body served as application/json but is actually HTML must degrade, not raise
    # (R-M7): a lenient caller gets a not-ok selection, not a JSONDecodeError.
    httpserver.expect_request("/badjson").respond_with_data(
        b"<html><body>not json at all</body></html>", content_type="application/json"
    )
    doc = wc.fetch(httpserver.url_for("/badjson"))
    assert doc.kind == "json"
    assert doc.text_content == "null"  # no crash: empty data
    assert not doc.select("anything", optional=True).ok


def test_search_raises_on_a_failed_results_page(httpserver, wc):
    # search is loud by default: a not-ok results page raises, never a silent [] .
    from webclient import RETURN, WebException

    httpserver.expect_request("/s").respond_with_data("err", status=503)
    url = httpserver.url_for("/s")
    with pytest.raises(WebException):
        wc.search("q", endpoint=url)
    assert wc.search("q", endpoint=url, optional=True) == []   # opt-in lenient -> []
    assert wc.search("q", endpoint=url, error=RETURN) == []


def test_not_operator_evaluates(httpserver, wc):
    # `~expr` records op "not"; regression: it used to KeyError (500) at execution.
    page = b'<html><body><div class="item">a</div>' \
           b'<div class="item"><span class="hide">x</span>b</div></body></html>'
    httpserver.expect_request("/n").respond_with_data(page, content_type="text/html")
    # keep only items with NO .hide child (the ~ predicate must evaluate)
    kept = (
        wc.fetch(httpserver.url_for("/n"))
        .select_all(".item")
        .filter(~wq.doc.select(".hide", optional=True).is_ok())
        .project()
    )
    assert len(kept) == 1  # the first item (no .hide) survived
