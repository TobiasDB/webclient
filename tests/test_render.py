from pathlib import Path

import pytest

from webclient import Document, Element, Reference

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


def test_markdown_renders_tables_as_gfm():
    html = (
        b"<html><body><table>"
        b"<tr><th>Weight</th><th>Material</th></tr>"
        b"<tr><td>200g</td><td>BPA-free</td></tr>"
        b"</table></body></html>"
    )
    md = Document(content=html, status_code=200).render("markdown")
    assert "| Weight | Material |" in md
    assert "| --- | --- |" in md
    assert "| 200g | BPA-free |" in md


def test_markdown_renders_nested_lists_with_indentation():
    html = (
        b"<html><body><ul>"
        b"<li>one</li><li>two<ul><li>nested-a</li><li>nested-b</li></ul></li>"
        b"</ul></body></html>"
    )
    md = Document(content=html, status_code=200).render("markdown")
    assert "- one" in md and "- two" in md
    assert "  - nested-a" in md and "  - nested-b" in md  # indented, not concatenated
    assert "twonested" not in md


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


def test_custom_backing_overrides_builtin_render():
    """The extensibility hook is ``wc.use(backing)``: a registered backing is
    chosen before the built-ins for the cores it applies to, so an HtmlBacking
    subclass can override one render format and ``super()`` the rest."""
    from webclient import HtmlBacking, WebClient

    class Upper(HtmlBacking):
        provides = frozenset({"render"})  # override render only

        def render(self, core, format, **options):
            if format == "markdown":
                return "UPPER"
            return super().render(core, format, **options)

    with WebClient() as wc:
        wc.use(Upper())
        doc = make_doc()
        doc._client = wc.core  # a core (the surface IS a core now; no unwrapping)
        assert doc.render("markdown") == "UPPER"  # overridden
        assert "<html" in doc.render("html").lower()  # other formats: super()
        assert doc.select("h1").text_content == "Big News"  # select still built-in


def test_latest_registered_backing_wins():
    """Two backings overriding the same op: the most recently registered wins
    (chosen first)."""
    from webclient import HtmlBacking, WebClient

    class Upper(HtmlBacking):
        provides = frozenset({"render"})

        def render(self, core, format, **options):
            return "UPPER" if format == "markdown" else super().render(core, format)

    class Lower(HtmlBacking):
        provides = frozenset({"render"})

        def render(self, core, format, **options):
            return "lower" if format == "markdown" else super().render(core, format)

    with WebClient() as wc:
        wc.use(Upper())
        wc.use(Lower())  # newest -> wins
        doc = make_doc()
        doc._client = wc.core
        assert doc.render("markdown") == "lower"
