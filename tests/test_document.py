from pathlib import Path

import pytest

from webclient import (
    ActionEvent,
    BinaryDocument,
    DOMUpdateEvent,
    Document,
    HTMLDocument,
    JSONDocument,
    NetworkEvent,
    Reference,
    XMLDocument,
)

FIXTURES = Path(__file__).parent / "fixtures"


def make_doc(**overrides) -> Document:
    values = dict(
        hostname="example.com",
        path="/list",
        kind="html",
        content=(FIXTURES / "page.html").read_bytes(),
        status_code=200,
    )
    values.update(overrides)
    return Document(**values)


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

def test_views_are_typed_cached_and_alias_the_document():
    doc = make_doc()
    view = doc.html
    assert isinstance(view, HTMLDocument)
    assert doc.html is view                    # cached
    assert view.html is view                   # already the right type
    assert view.events is doc.events           # shared event store
    doc.events.append(ActionEvent(action="click"))
    assert view.events_of(ActionEvent)         # visible through the view
    assert view.bound is doc.bound             # shared private state


# -- selection: css and xpath, elements only (ISSUES #10) ------------------- #

def test_select_css():
    assert make_doc().select(".card .title").text == "First Card"


def test_select_xpath():
    node = make_doc().select('//div[@class="card"][2]//h2')
    assert node.text == "Second Card"


def test_select_index_and_negative_index():
    doc = make_doc()
    assert make_doc().select(".card", index=1).attr("data-rank") == "2"
    assert doc.select(".card", index=-1).attr("data-rank") == "3"


def test_select_missing_raises_unless_optional():
    doc = make_doc()
    with pytest.raises(LookupError):
        doc.select(".nope")
    assert doc.select(".nope", optional=True) is None


def test_select_rejects_attribute_and_text_xpath():
    doc = make_doc()
    with pytest.raises(ValueError, match="attr"):
        doc.select("//a/@href")
    with pytest.raises(ValueError, match="attr"):
        doc.select("//a/text()")


def test_select_all_limit_offset():
    doc = make_doc()
    assert len(doc.select_all(".card")) == 3
    ranks = [n.attr("data-rank") for n in doc.select_all(".card", limit=2, offset=1)]
    assert ranks == ["2", "3"]


def test_select_on_treeless_kind_raises_typed_error():
    doc = make_doc(kind="json", content=b"{}")
    with pytest.raises(TypeError, match="json"):
        doc.select(".card")


# -- nodes ------------------------------------------------------------------ #

def test_node_text_is_whitespace_normalized():
    assert make_doc().select(".card .title").text == "First Card"


def test_node_select_is_scoped_to_element():
    card = make_doc().select(".card", index=2)
    assert card.select(".status").text == "Inactive"


def test_attr_missing_raises_unless_optional():
    node = make_doc().select(".card .title")
    with pytest.raises(LookupError):
        node.attr("data-nope")
    assert node.attr("data-nope", optional=True) is None


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
    page = make_doc().html
    assert page.title == "Fixture Page"
    links = page.links()
    assert len(links) == 3
    assert all(isinstance(ref, Reference) for ref in links)


# -- other kinds ------------------------------------------------------------ #

def test_json_data():
    doc = JSONDocument(hostname="e.com", content=b'{"a": [1, 2]}', status_code=200)
    assert doc.data == {"a": [1, 2]}


def test_xml_selection_and_lenient_parse():
    xml = b"<feed><entry><title>One</title></entry><entry><title>Two</title>"
    doc = XMLDocument(hostname="e.com", content=xml, status_code=200)
    titles = [n.text for n in doc.select_all("//entry/title")]
    assert titles == ["One", "Two"]  # unclosed tags recovered
    assert doc.select("entry title").text == "One"  # css works too


def test_binary_save(tmp_path):
    doc = BinaryDocument(hostname="e.com", content=b"\x00\x01", status_code=200)
    out = doc.save(str(tmp_path / "blob.bin"))
    assert Path(out).read_bytes() == b"\x00\x01"


# -- event store ------------------------------------------------------------ #

def test_events_of_by_class_and_topic_prefix():
    doc = make_doc()
    doc.events.append(ActionEvent(action="click"))
    doc.events.append(DOMUpdateEvent(kind="added"))
    assert [e.action for e in doc.actions] == ["click"]
    assert len(doc.events_of("dom")) == 1          # prefix matches dom.update
    assert len(doc.events_of("dom.update")) == 1
    assert doc.events_of("domx") == []             # not a prefix match
    assert len(doc.events_of(DOMUpdateEvent)) == 1


def test_node_events_are_document_scoped():
    doc = make_doc()
    doc.events.append(ActionEvent(action="click"))
    node = doc.select(".card")
    assert len(node.events_of(ActionEvent)) == 1   # ISSUES #9: static = doc scope


def test_network_event_forward_ref_resolved():
    event = NetworkEvent(request=Reference(hostname="e.com"), document_id="d1")
    assert event.topic == "network"


# -- later milestones stay loud --------------------------------------------- #

def test_unbuilt_features_raise_with_milestone():
    from webclient import WebClient
    with WebClient() as wc:
        with pytest.raises(NotImplementedError, match="M6"):
            wc.execute(object(), make_doc())
