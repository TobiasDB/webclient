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
    from webclient.document import apply_status
    return apply_status(Document(**values))


# -- decoding / status ------------------------------------------------------ #

def test_text_uses_declared_encoding_first():
    doc = make_doc(content="café".encode("latin-1"), encoding="latin-1")
    assert doc.text == "café"


def test_text_detects_encoding_when_undeclared():
    doc = make_doc(content="décor première café".encode("latin-1"), encoding=None)
    assert "café" in doc.text


def test_ok_is_2xx():
    assert make_doc(status_code=204).ok
    assert not make_doc(status_code=404).ok


# -- typed views (ISSUES #6 aliasing pin) ----------------------------------- #

def test_select_css():
    assert make_doc().select(".card .title").text == "First Card"


def test_select_xpath():
    node = make_doc().select('//div[@class="card"][2]//h2')
    assert node.text == "Second Card"


def test_select_index_and_negative_index():
    doc = make_doc()
    assert make_doc().select(".card", index=1).attr("data-rank").get() == "2"
    assert doc.select(".card", index=-1).attr("data-rank") == "3"   # Field == -> Field[bool], truthy eagerly


def test_select_missing_raises_unless_policy_returns():
    doc = make_doc()
    with pytest.raises(LookupError):
        doc.select(".nope")
    missing = doc.select(".nope", error=RETURN)
    assert not missing.ok and missing.error.type == "LookupError"
    assert missing.root == doc.name or missing.root is None


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
    lifted = cards.attr("data-rank")                       # element op -> Collection
    assert len(lifted) == 2 and [f.get() for f in lifted] == ["2", "3"]


def test_select_on_treeless_kind_raises_typed_error():
    doc = make_doc(kind="binary", content=b"\x00")
    with pytest.raises(TypeError, match="requires 'tree'"):
        doc.select(".card")


def test_json_select_and_attr_value():
    doc = make_doc(kind="json", content=b'{"items": [{"n": 1}, {"n": 2}], "name": "x"}')
    assert doc.select("name").attr("value").get() == "x"
    assert [d.attr("n").get() for d in doc.select_all("items")] == [1, 2]
    assert doc.select("items[1].n").text == "2"


# -- elements are documents (P3) -------------------------------------------- #

def test_element_text_is_whitespace_normalized():
    el = make_doc().select(".card .title")
    assert el.text == "First Card" and el.attr("text").get() == "First Card"
    assert isinstance(el, Document) and el.kind == "html"
    assert el.content.startswith(b"<h2")                # element bytes


def test_element_select_is_scoped_to_element():
    card = make_doc().select(".card", index=2)
    assert card.select(".status").text == "Inactive"
    assert card.status_code == 200 and card.url == "https://example.com/list"


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
    doc = Document(kind="json", hostname="e.com", content=b'{"a": [1, 2]}', status_code=200)
    import json as _j
    assert _j.loads(doc.text) == {"a": [1, 2]}


def test_xml_selection_and_lenient_parse():
    xml = b"<feed><entry><title>One</title></entry><entry><title>Two</title>"
    doc = Document(kind="xml", hostname="e.com", content=xml, status_code=200)
    titles = [n.text for n in doc.select_all("//entry/title")]
    assert titles == ["One", "Two"]  # unclosed tags recovered
    assert doc.select("entry title").text == "One"  # css works too


def test_events_of_by_class_and_topic_prefix():
    doc = make_doc()
    doc.events.append(ActionEvent(action="click"))
    doc.events.append(DOMUpdateEvent(kind="added"))
    assert [e.action for e in doc.action_events] == ["click"]
    assert len(doc.events_of("dom")) == 1          # prefix matches dom.update
    assert len(doc.events_of("dom.update")) == 1
    assert doc.events_of("domx") == []             # not a prefix match
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
