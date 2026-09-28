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


def test_markup_reads_are_safe_on_non_markup_and_bad_bytes() -> None:
    # a JSON document: markup reads return empty, not raise
    j = parse(b'{"a": 1}', content_type="application/json")
    assert j.kind == "json" and j.select("a") is None and j.select_all("a") == [] and j.links() == []
    assert j.json() == {"a": 1}

    # malformed XML recovers to a partial tree -- select does not crash
    bad_xml = parse(b"<root><item>1</item><item>2", content_type="application/xml")
    assert bad_xml.kind == "xml" and len(bad_xml.select_all("item")) == 2

    # empty content -> empty tree, empty reads
    empty = parse(b"", content_type="text/html")
    assert empty.select("div") is None and empty.select_all("div") == [] and empty.links() == []


# -- content extraction: metadata, readable/markdown, tables, regex, structure --

_PAGE = b"""<!doctype html><html><head>
  <title>  My  Page </title>
  <meta name="description" content="a demo">
  <meta property="og:title" content="OG Title">
  <link rel="canonical" href="/canonical">
  <link rel="alternate" type="application/rss+xml" href="/feed.xml">
  <script type="application/ld+json">{"@type":"Article","name":"X"}</script>
</head><body>
  <nav><a href="/home">Home</a></nav>
  <main>
    <h1>Heading One</h1>
    <p>Read <a href="/more">more</a> here.</p>
    <h2>Sub</h2>
  </main>
  <footer>copyright</footer>
</body></html>"""


def test_metadata_reads_head_facts() -> None:
    doc = parse(_PAGE, content_type="text/html", url="https://ex.com/dir/")
    m = doc.metadata()
    assert m.title == "My Page"
    assert m.description == "a demo"
    assert m.canonical == "https://ex.com/canonical"      # resolved absolute
    assert m.og["og:title"] == "OG Title"
    assert m.feeds == ["https://ex.com/feed.xml"]
    assert m.ld_json == [{"@type": "Article", "name": "X"}]


def test_readable_strips_chrome_to_main() -> None:
    doc = parse(_PAGE, content_type="text/html", url="https://ex.com/")
    txt = doc.readable()  # main_content_only default
    assert "Heading One" in txt and "Read more here." in txt
    assert "Home" not in txt and "copyright" not in txt   # nav + footer dropped


def test_markdown_renders_headings_and_links() -> None:
    doc = parse(_PAGE, content_type="text/html", url="https://ex.com/")
    md = doc.markdown(main_content_only=True)
    assert "# Heading One" in md and "## Sub" in md
    assert "[more](https://ex.com/more)" in md


def test_region_reports_the_landmark() -> None:
    doc = parse(_PAGE, content_type="text/html", url="https://ex.com/")
    assert doc.select_all("nav a")[0].region == "nav"
    assert doc.select_all("main a")[0].region == "main"


def test_outline_is_the_heading_tree() -> None:
    doc = parse(_PAGE, content_type="text/html")
    out = doc.outline()
    assert [(h.level, h.text) for h in out] == [(1, "Heading One"), (2, "Sub")]


def test_tables_expand_rowspan_into_records() -> None:
    html = b"""<table>
      <tr><th>Region</th><th>City</th></tr>
      <tr><td rowspan="2">West</td><td>SF</td></tr>
      <tr><td>LA</td></tr>
    </table>"""
    doc = parse(html, content_type="text/html")
    rows = doc.tables()
    assert rows == [{"Region": "West", "City": "SF"}, {"Region": "West", "City": "LA"}]


def test_tables_transpose_keys_by_first_column() -> None:
    html = b"<table><tr><td>Feature</td><td>A</td><td>B</td></tr><tr><td>Price</td><td>1</td><td>2</td></tr></table>"
    doc = parse(html, content_type="text/html")
    rows = doc.tables(transpose=True)
    assert rows == [{"Feature": "A", "Price": "1"}, {"Feature": "B", "Price": "2"}]


def test_regex_extracts_from_free_text() -> None:
    doc = parse(b"<p>Order #4821 total $19.99 and #4822</p>", content_type="text/html")
    assert doc.regex(r"#(\d+)", group=1) == "4821"
    assert doc.regex_all(r"#(\d+)", group=1) == ["4821", "4822"]
    assert doc.regex(r"nope") is None


def test_skeleton_keeps_semantics_drops_noise() -> None:
    html = b'<div id="app" class="grid css-1a2b3c"><script>var x=1</script><span class="price">$5</span></div>'
    doc = parse(html, content_type="text/html")
    sk = doc.skeleton()
    assert "div#app.grid" in sk           # id + semantic class kept
    assert "css-1a2b3c" not in sk         # hashed build class dropped
    assert "script" not in sk             # noise subtree skipped
    assert "span.price" in sk
