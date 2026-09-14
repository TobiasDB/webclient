from pathlib import Path

import pytest

from webclient import Document, Element, Reference, Renderer

FIXTURES = Path(__file__).parent / "fixtures"

ARTICLE = b"""
<html><head><title>T</title><script>x()</script></head><body>
<nav><a href="/home">home</a></nav>
<main>
  <h1>Big News</h1>
  <p>First <strong>bold</strong> paragraph with a
     <a href="/more">link</a>.</p>
  <ul><li>one</li><li>two</li></ul>
  <pre>code block</pre>
  <img src="/pic.png" alt="A picture">
</main>
<footer>fine print</footer>
</body></html>
"""


def make_doc() -> Document:
    return Document(hostname="e.com", path="/a", content=ARTICLE, status_code=200)


def test_markdown_renderer():
    md = make_doc().render("markdown")
    assert "# Big News" in md
    assert "First **bold** paragraph" in md
    assert "[link](/more)" in md
    assert "- one\n- two" in md
    assert "```\ncode block\n```" in md
    assert "![A picture](/pic.png)" in md
    assert "x()" not in md  # scripts stripped


def test_markdown_via_view_sugar():
    assert "# Big News" in make_doc().render("markdown")


def test_text_renderer_is_readable():
    text = make_doc().render("text")
    assert "Big News" in text and "First bold paragraph" in text
    assert "fine print" not in text  # footer is noise
    assert "home" not in text  # nav is noise


def test_elements_renderer_sections_under_titles():
    elements = make_doc().render("elements")
    types = [e.type for e in elements]
    assert types == ["title", "text", "list_item", "list_item", "code", "image"]
    title = elements[0]
    assert all(e.parent_id == title.id for e in elements[1:])
    assert elements[-1].metadata["src"] == "/pic.png"


def test_links_and_html_formats():
    doc = make_doc()
    links = doc.render("links")
    assert [ref.path for ref in links] == ["/home", "/more"]
    assert isinstance(list(links)[0], Reference)
    assert "<main>" in doc.render("html")


def test_json_elements_renderer():
    doc = Document(
        kind="json",
        hostname="e.com",
        status_code=200,
        content=b'{"a": {"b": 1}, "c": [true, "x"]}',
    )
    elements = doc.render("elements")
    by_id = {e.id: e for e in elements}
    assert by_id["a.b"].text == "1" and by_id["a.b"].parent_id == "a"
    assert by_id["c[1]"].text == "x"


def test_json_query():
    doc = Document(
        kind="json",
        hostname="e.com",
        status_code=200,
        content=b'{"items": [{"name": "n0"}, {"name": "n1"}]}',
    )
    assert doc.select("items[1].name").text_content == "n1"


def test_unknown_format_raises():
    with pytest.raises(LookupError, match="pdf"):
        make_doc().render("pdf")


def test_custom_renderer_overrides_backing_builtin(caplog):
    """A custom Renderer for a (kind, format) overrides the backing's built-in
    render. Core rendering is now a backing (not a registered renderer), so
    nothing is shadowed and no warning is logged."""
    from webclient import WebClient

    class Upper(Renderer):
        name: str = "upper"
        kind: str = "html"  # type: ignore[assignment]
        formats: list[str] = ["markdown"]

        def render(self, document, format, **options):
            return "UPPER"

    with WebClient() as wc:
        with caplog.at_level("WARNING", logger="webclient"):
            wc.use(Upper())
        assert not any("shadows" in r.message for r in caplog.records)
        doc = make_doc()
        doc._client = wc
        assert doc.render("markdown") == "UPPER"


def test_second_custom_renderer_shadows_first_with_warning(caplog):
    """Two custom renderers claiming the same (kind, format): the second
    shadows the first, and that shadowing is warned (ISSUES #16)."""
    from webclient import WebClient

    class Upper(Renderer):
        name: str = "upper"
        kind: str = "html"  # type: ignore[assignment]
        formats: list[str] = ["markdown"]

        def render(self, document, format, **options):
            return "UPPER"

    class Lower(Upper):
        name: str = "lower"

        def render(self, document, format, **options):
            return "lower"

    with WebClient() as wc:
        wc.use(Upper())
        with caplog.at_level("WARNING", logger="webclient"):
            wc.use(Lower())
        assert any("shadows" in r.message for r in caplog.records)
        doc = make_doc()
        doc._client = wc
        assert doc.render("markdown") == "lower"
