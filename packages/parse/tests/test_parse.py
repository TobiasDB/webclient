"""web.parse tests -- interpretation in isolation (parse_bytes needs no fetch)."""

from __future__ import annotations

from web.fetch import Request, Snapshot
from web.parse import Document, parse, parse_bytes


def test_sniff_and_select_html() -> None:
    doc = parse_bytes(
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
    doc = parse_bytes(b'{"a": [1, 2], "b": "x"}', content_type="application/json")
    assert doc.kind == "json"
    assert doc.json() == {"a": [1, 2], "b": "x"}


def test_sniff_from_bytes_without_content_type() -> None:
    assert parse_bytes(b"[1,2,3]").kind == "json"
    assert parse_bytes(b"<!doctype html><html></html>").kind == "html"
    assert parse_bytes(b"\x89PNG\r\n\x1a\n\x00\x01").kind == "binary"
    assert parse_bytes(b"just words").kind == "text"


def test_parse_from_snapshot_carries_fields() -> None:
    snap = Snapshot(
        request=Request(url="https://ex.com/x"),
        url="https://ex.com/final",
        status=200,
        headers={"Content-Type": "text/html"},  # mixed case -> looked up case-insensitively
        content=b"<html><title>T</title></html>",
    )
    doc = parse(snap)
    assert doc.kind == "html" and doc.url == "https://ex.com/final" and doc.status == 200
    assert doc.ok and doc.select_all("title")[0].text == "T"


def test_nested_select_composes() -> None:
    doc = parse_bytes(b"<ul><li><a href='/a'>A</a></li><li><b>B</b></li></ul>", content_type="text/html")
    lis = doc.select_all("li")
    assert len(lis) == 2
    assert lis[0].select_all("a")[0].text == "A"  # select on an Element's subtree
