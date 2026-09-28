"""web.parse tests -- interpretation in isolation (parse needs no fetch, only bytes)."""

from __future__ import annotations

from web.parse import Document, parse


def test_sniff_and_select_html() -> None:
    doc = parse(
        b"<html><body><a href='/p'>x</a><p class=q>Hello  world</p></body></html>",
        content_type="text/html; charset=utf-8",
        url="https://ex.com/dir/",
    )
    assert isinstance(doc, Document) and doc.kind == "html"
    ps = doc.select_all("p.q")
    assert len(ps) == 1 and ps[0].text == "Hello world"  # whitespace collapsed
    assert doc.links() == ["https://ex.com/p"]  # href resolved absolute
    assert doc.select_all("a")[0].attr("href") == "https://ex.com/p"


def test_sniff_json_and_read_value() -> None:
    doc = parse(b'{"a": [1, 2], "b": "x"}', content_type="application/json")
    assert doc.kind == "json"
    assert doc.json() == {"a": [1, 2], "b": "x"}


def test_sniff_from_bytes_without_content_type() -> None:
    assert parse(b"[1,2,3]").kind == "json"
    assert parse(b"<!doctype html><html></html>").kind == "html"
    assert parse(b"\x89PNG\r\n\x1a\n\x00\x01").kind == "binary"
    assert parse(b"just words").kind == "text"


def test_document_is_pure_content_no_transport_facts() -> None:
    # parse needs only bytes + an optional base url (for links); NO status/headers/error
    doc = parse(b"<title>T</title>", content_type="text/html", url="https://ex.com/x")
    assert doc.kind == "html" and doc.url == "https://ex.com/x"
    assert doc.select("title") is not None and doc.select("title").text == "T"  # type: ignore[union-attr]
    assert not hasattr(doc, "status") and not hasattr(doc, "ok") and not hasattr(doc, "error")


def test_nested_select_composes() -> None:
    doc = parse(b"<ul><li><a href='/a'>A</a></li><li><b>B</b></li></ul>", content_type="text/html")
    lis = doc.select_all("li")
    assert len(lis) == 2
    assert lis[0].select_all("a")[0].text == "A"  # select on an Element's subtree
