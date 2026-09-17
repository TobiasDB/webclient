from pathlib import Path

import pytest

from webclient import (
    RETURN,
    RETURN,
    ActionEvent,
    DOMUpdateEvent,
    Document,
    NetworkEvent,
    Reference,
)

FIXTURES = Path(__file__).parent / "fixtures"


def make_doc(**overrides) -> Document:
    values = dict(
        url="https://example.com/list",
        kind="html",
        content=(FIXTURES / "page.html").read_bytes(),
        status_code=200,
    )
    values.update(overrides)
    return Document(**values)


# -- decoding / status ------------------------------------------------------ #


def test_text_uses_declared_encoding_first():
    doc = make_doc(content="café".encode("latin-1"), encoding="latin-1")
    assert doc.text_content == "café"


def test_text_detects_encoding_when_undeclared():
    doc = make_doc(content="décor première café".encode("latin-1"), encoding=None)
    assert "café" in doc.text_content


def test_ok_is_2xx():
    assert make_doc(status_code=204).ok
    assert not make_doc(status_code=404).ok


# -- typed views (ISSUES #6 aliasing pin) ----------------------------------- #


def test_select_css():
    assert make_doc().select(".card .title").text_content == "First Card"


def test_select_xpath():
    node = make_doc().select('//div[@class="card"][2]//h2')
    assert node.text_content == "Second Card"


def test_select_index_and_negative_index():
    doc = make_doc()
    assert make_doc().select(".card", index=1).attr("data-rank").get() == "2"
    assert (
        doc.select(".card", index=-1).attr("data-rank") == "3"
    )  # Field == -> Field[bool], truthy eagerly


def test_select_missing_raises_unless_policy_returns():
    doc = make_doc()
    with pytest.raises(LookupError):
        doc.select(".nope")
    missing = doc.select(".nope", error=RETURN)
    assert not missing.ok and missing.error.type == "LookupError"
    assert missing.root == doc.name or missing.root is None


def test_empty_select_all_is_a_collection_not_a_bare_list():
    # a select_all that matches nothing must still be a Collection, so the headline
    # extract/project pattern doesn't AttributeError on a zero-match page.
    from webclient.collection import Collection

    from webclient import doc as ldoc  # the lazy authoring root

    d = make_doc()
    empty = d.select_all(".no-such-thing")
    assert isinstance(empty, Collection) and len(empty) == 0
    # the row-shaping ops apply (and yield []) instead of AttributeError-ing
    assert empty.extract(t=ldoc.select(".title").text_content).project() == []


def test_empty_links_is_a_collection():
    doc = make_doc(content=b"<html><body><p>no links here</p></body></html>")
    assert doc.links().project() == []  # Collection, not a bare list


def test_select_miss_is_a_structured_webexception():
    # one `except WebException` now covers fetch failures AND selection misses,
    # and it stays a LookupError for back-compat.
    from webclient import WebException
    from webclient.errors import SelectError

    doc = make_doc()
    with pytest.raises(WebException) as ei:
        doc.select(".nope")
    exc = ei.value
    assert isinstance(exc, SelectError) and isinstance(exc, LookupError)
    assert exc.error.type == "LookupError" and exc.error.retriable is False
    assert "nope" in exc.error.message


def test_optional_is_a_universal_lenient_spelling():
    # `optional=True` returns a not-ok result on select/attr (same as error=RETURN)
    doc = make_doc()
    assert not doc.select(".nope", optional=True).ok
    node = doc.select(".card .title")
    assert node.attr("data-nope", optional=True).ok is False


def test_link_attr_on_a_missing_element_is_a_reference_not_a_field():
    # E-M1: attr("href") is typed Reference; on a miss it must be an (empty) not-ok
    # Reference, so `.url` works -- never a Field that would AttributeError on .url.
    from webclient import Reference

    doc = make_doc()
    ref = doc.select(".nope", optional=True).attr("href")
    assert isinstance(ref, Reference) and ref.hostname == ""  # empty, not a Field
    assert isinstance(ref.url, str)  # .url works (a Field would AttributeError)


def test_select_rejects_attribute_and_text_xpath():
    doc = make_doc()
    with pytest.raises(ValueError, match="attr"):
        doc.select("//a/@href")
    with pytest.raises(ValueError, match="attr"):
        doc.select("//a/text()")


def test_select_all_limit_offset():
    doc = make_doc()
    assert len(doc.select_all(".card")) == 3
    cards = doc.select_all(".card", limit=2, offset=1)
    assert [n.attr("data-rank").get() for n in cards] == ["2", "3"]
    lifted = cards.attr("data-rank")  # element op -> Collection
    assert len(lifted) == 2 and [f.get() for f in lifted] == ["2", "3"]


def test_select_on_treeless_kind_raises_typed_error():
    doc = make_doc(kind="binary", content=b"\x00")
    with pytest.raises(
        TypeError, match="not available"
    ):  # UnsupportedOp: no tree backing
        doc.select(".card")


def test_json_select_and_attr_value():
    doc = make_doc(kind="json", content=b'{"items": [{"n": 1}, {"n": 2}], "name": "x"}')
    assert doc.select("name").attr("value").get() == "x"
    assert [d.attr("n").get() for d in doc.select_all("items")] == [1, 2]
    assert doc.select("items[1].n").text_content == "2"


def test_skeleton_drops_hashed_classes_and_optionally_chrome():
    html = (
        b'<html><body>'
        b'<nav class="site-nav"><a href="/">Home</a></nav>'
        b'<main><article class="post css-1a2b3c AMTIxG_grid jsx-1837462">'
        b'<h2 class="post-title emotion-9xk2">Hello</h2></article></main>'
        b'<footer class="site-footer"><a href="/tos">Terms</a></footer>'
        b'</body></html>'
    )
    doc = make_doc(content=html)
    skel = doc.skeleton()
    # semantic classes kept, high-entropy build classes dropped
    assert "post-title" in skel and "post" in skel
    assert "css-1a2b3c" not in skel and "AMTIxG_grid" not in skel and "emotion-9xk2" not in skel
    # by default the chrome is present; drop_chrome removes nav/footer landmarks
    assert "site-nav" in skel and "site-footer" in skel
    lean = doc.skeleton(drop_chrome=True)
    assert "site-nav" not in lean and "site-footer" not in lean
    assert "post-title" in lean  # the record survives


def test_xml_element_select_is_case_insensitive_fallback():
    # XML tag names are case-sensitive, but scrapers/LLMs lowercase them and RSS/Atom feeds
    # spell them pubDate/lastBuildDate/... . A lowercased bare tag must still resolve.
    rss = (b"<?xml version='1.0'?><rss><channel>"
           b"<item><title>A</title><pubDate>Mon, 01 Sep 2026</pubDate><guid>g-1</guid></item>"
           b"<item><title>B</title><pubDate>Tue, 02 Sep 2026</pubDate><guid>g-2</guid></item>"
           b"</channel></rss>")
    doc = make_doc(kind="xml", content=rss)
    items = doc.select_all("item")
    assert len(items) == 2
    # exact case AND lowercased both resolve the mixed-case <pubDate>
    assert items[0].select("pubDate").attr("text").get() == "Mon, 01 Sep 2026"
    assert items[0].select("pubdate").attr("text").get() == "Mon, 01 Sep 2026"
    # a genuinely absent tag still misses (the fallback doesn't invent matches)
    assert not items[0].select("author", optional=True).ok


def test_as_json_reparses_an_injected_json_island():
    # data injected into the page as a <script type=application/json> blob, not as DOM:
    # select the script, .as_json() reparses its text into a JSON document, then dotted-path.
    html = (b'<html><body><div id="grid"></div>'
            b'<script id="__DATA__" type="application/json">'
            b'{"catalog": {"items": [{"sku": "A-1"}, {"sku": "B-2"}]}}'
            b"</script></body></html>")
    doc = make_doc(content=html)  # kind html
    data = doc.select("script#__DATA__").as_json()
    assert data.kind == "json"
    assert [d.attr("sku").get() for d in data.select_all("catalog.items")] == ["A-1", "B-2"]
    # a missed select .as_json() is a not-ok document, not a crash
    assert not doc.select("script#nope", optional=True).as_json().ok


# -- elements are documents (P3) -------------------------------------------- #


def test_element_text_is_whitespace_normalized():
    el = make_doc().select(".card .title")
    assert el.text_content == "First Card"
    assert isinstance(el, Document) and el.kind == "html"
    # a selected element's markup is serialised on demand via html() (its .content --
    # the raw response bytes -- is empty; the element isn't a fetched response).
    assert el.html().startswith("<h2")


def test_element_select_is_scoped_to_element():
    card = make_doc().select(".card", index=2)
    assert card.select(".status").text_content == "Inactive"
    assert card.status_code == 200 and card.url == "https://example.com/list"


def test_href_whitespace_is_cleaned_into_a_valid_url():
    # a template's newlines, padding, or a text-like href (`<a href="Read More">`)
    # must not build an un-fetchable URL with raw whitespace -- browsers strip
    # surrounding whitespace, drop internal tab/newline, and %20-encode spaces.
    page = (
        "<html><body>"
        '<a id="txt" href="Read More">t</a>'
        '<a id="pad" href="  /path ">p</a>'
        '<a id="nl" href="\n  /news\n">n</a>'
        "</body></html>"
    )
    doc = make_doc(content=page.encode())
    assert doc.select("#txt").attr("href").url.endswith("/Read%20More")
    assert doc.select("#pad").attr("href").url.endswith("/path")  # padding stripped
    assert doc.select("#nl").attr("href").url.endswith("/news")   # newlines dropped
    for r in doc.links():  # the links() render is cleaned the same way
        assert " " not in r.url and "\n" not in r.url


def test_attr_is_the_universal_accessor_text_and_html():
    # attr("text") == text_content (no separate op to remember, no silent miss on a
    # "text" attribute); attr("html") is the element's markup.
    doc = make_doc(content=b'<html><body><p class="x">Hi <b>there</b></p></body></html>')
    p = doc.select(".x")
    assert p.attr("text").get() == p.text_content == "Hi there"
    assert "<b>there</b>" in p.attr("html").get()
    # still resolves a real attribute
    assert p.attr("class").get() == "x"


def test_region_reports_the_landmark_an_element_sits_in():
    page = (
        "<html><body>"
        '<nav><a id="n" href="/x">nav</a></nav>'
        '<main><article><a id="a" href="/y">art</a></article></main>'
        '<div role="contentinfo"><a id="f" href="/z">foot</a></div>'
        '<aside class="sidebar"><a id="s" href="/w">side</a></aside>'
        '<a id="none" href="/q">loose</a>'
        "</body></html>"
    )
    doc = make_doc(content=page.encode())
    assert doc.select("#n").region == "nav"
    assert doc.select("#a").region == "article"  # nearest landmark wins
    assert doc.select("#f").region == "footer"  # via ARIA role
    assert doc.select("#s").region == "aside"  # via class hint
    assert doc.select("#none").region == ""  # no landmark ancestor


def test_attr_missing_raises_unless_policy_returns():
    node = make_doc().select(".card .title")
    with pytest.raises(LookupError):
        node.attr("data-nope")
    missing = node.attr("data-nope", error=RETURN)
    assert not missing.ok and missing.value is None


def test_attr_href_resolves_to_reference_against_document():
    doc = make_doc()
    absolute = doc.select(".card", index=0).select("a").attr("href")
    relative = doc.select(".card", index=2).select("a").attr("href")
    external = doc.select(".card", index=1).select("a").attr("href")
    assert isinstance(absolute, Reference)
    assert absolute.path == "/items/1" and absolute.params == {"ref": "home"}
    assert relative.url == "https://example.com/items/3"
    assert external.hostname == "other.example"


# -- html sugar ------------------------------------------------------------- #


def test_title_and_links():
    page = make_doc()
    assert page.title == "Fixture Page"
    links = page.render("links")
    assert len(links) == 3
    assert all(isinstance(ref, Reference) for ref in links)


# -- other kinds ------------------------------------------------------------ #


def test_json_data():
    doc = Document(
        kind="json", hostname="e.com", content=b'{"a": [1, 2]}', status_code=200
    )
    import json as _j

    assert _j.loads(doc.text_content) == {"a": [1, 2]}


def test_xml_selection_and_lenient_parse():
    xml = b"<feed><entry><title>One</title></entry><entry><title>Two</title>"
    doc = Document(kind="xml", hostname="e.com", content=xml, status_code=200)
    titles = [n.text_content for n in doc.select_all("//entry/title")]
    assert titles == ["One", "Two"]  # unclosed tags recovered
    assert doc.select("entry title").text_content == "One"  # css works too


def test_events_of_by_class_and_topic_prefix():
    doc = make_doc()
    doc.events.append(ActionEvent(action="click"))
    doc.events.append(DOMUpdateEvent(kind="added"))
    assert [e.action for e in doc.action_events] == ["click"]
    assert len(doc.events_of("dom")) == 1  # prefix matches dom.update
    assert len(doc.events_of("dom.update")) == 1
    assert doc.events_of("domx") == []  # not a prefix match
    assert len(doc.events_of(DOMUpdateEvent)) == 1


def test_element_events_are_document_scoped():
    doc = make_doc()
    doc.events.append(ActionEvent(action="click"))
    # a static element shares the document's event store; node-id narrowing
    # only applies to a live element (which has a locator), ISSUES #9.
    assert len(doc.select(".card").events_of(ActionEvent)) == 1


def test_network_event_forward_ref_resolved():
    event = NetworkEvent(request=Reference(hostname="e.com"), document_id="d1")
    assert event.topic == "network"
