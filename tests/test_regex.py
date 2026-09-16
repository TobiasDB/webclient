"""RegexBacking: pattern extraction over a document's text."""

from webclient import Document, default_client


def _doc(html: bytes) -> Document:
    d = Document(content=html, kind="html", status_code=200)
    d._client = default_client()  # so loop-bridged ops (extract) work
    return d


def test_regex_first_match_and_capture_group():
    d = _doc(b'<span class="price">Price 30 unit $ per 1TB / month</span>')
    price = d.select(".price")
    assert price.regex(r"\d+").get() == "30"  # whole match (group 0)
    assert price.regex(r"(\d+)\s*unit", group=1).get() == "30"  # a capture group
    assert price.regex(r"unit\s*(\S+)", group=1).get() == "$"  # the unit


def test_regex_missing_is_an_empty_field():
    d = _doc(b"<p>nothing numeric here</p>")
    field = d.regex(r"\d+")
    assert not field.ok and field.is_empty()


def test_regex_all_returns_every_match():
    d = _doc(b"<ul><li>$10</li><li>$25</li><li>$3</li></ul>")
    assert d.regex_all(r"\$(\d+)", group=1) == ["10", "25", "3"]


def test_regex_flags():
    d = _doc(b"<p>HELLO world</p>")
    assert d.regex(r"hello", flags="i").get() == "HELLO"


def test_regex_in_a_nested_extract():
    # the motivating use: split a messy price string into value + unit as nested data.
    d = _doc(b'<div class="p"><span class="price">30 $ / 1TB</span></div>')
    row = d.select(".p").extract(
        value=d.select(".price").regex(r"[\d.]+"),
        unit=d.select(".price").regex(r"[\d.]+\s*(\S+)", group=1),
    ).project()
    assert row == {"value": "30", "unit": "$"}
