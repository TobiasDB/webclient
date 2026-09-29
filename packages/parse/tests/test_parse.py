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
    doc = parse(
        b"<ul><li><a href='/a'>A</a></li><li><b>B</b></li></ul>",
        content_type="text/html",
    )
    lis = doc.select_all("li")
    assert len(lis) == 2
    assert lis[0].select_all("a")[0].text == "A"  # select on an Element's subtree


def test_markup_reads_are_safe_on_non_markup_and_bad_bytes() -> None:
    # a JSON document: markup reads return empty, not raise
    j = parse(b'{"a": 1}', content_type="application/json")
    assert (
        j.kind == "json" and j.select("a") is None and j.select_all("a") == [] and j.links() == []
    )
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
    assert m.canonical == "https://ex.com/canonical"  # resolved absolute
    assert m.og["og:title"] == "OG Title"
    assert m.feeds == ["https://ex.com/feed.xml"]
    assert m.ld_json == [{"@type": "Article", "name": "X"}]


def test_readable_strips_chrome_to_main() -> None:
    doc = parse(_PAGE, content_type="text/html", url="https://ex.com/")
    txt = doc.readable()  # main_content_only default
    assert "Heading One" in txt and "Read more here." in txt
    assert "Home" not in txt and "copyright" not in txt  # nav + footer dropped


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
    html = b'<div id="app" class="catalog grid css-1a2b3c"><script>var x=1</script><span class="price">$5</span></div>'
    doc = parse(html, content_type="text/html")
    sk = doc.skeleton()
    assert "div#app.catalog" in sk  # id + semantic class kept
    assert "css-1a2b3c" not in sk  # hashed build class dropped
    assert "grid" not in sk  # tailwind utility class dropped
    assert "script" not in sk  # noise subtree skipped
    assert "span.price" in sk


# -- record-region detection + JSON navigation --


def test_find_records_locates_the_dataset_not_the_nav() -> None:
    html = b"""<html><body>
      <nav><a href=/1>Home</a><a href=/2>About</a><a href=/3>Contact</a><a href=/4>Blog</a></nav>
      <main><ul class=list>
        <li class=item><span class=t>A</span><span class=p>$1</span></li>
        <li class=item><span class=t>B</span><span class=p>$2</span></li>
        <li class=item><span class=t>C</span><span class=p>$3</span></li>
        <li class=item><span class=t>D</span><span class=p>$4</span></li>
      </ul></main>
    </body></html>"""
    doc = parse(html, content_type="text/html")
    regs = doc.records()
    assert regs, "expected at least one record region"
    top = regs[0]
    assert top.item_selector == "li.item" and top.count == 4  # the dataset, not the 4-link nav
    # the richer content list outranks the bare-link nav menu
    assert top.score >= max(r.score for r in regs)


def test_records_unwrap_anonymous_tailwind_wrappers() -> None:
    # each record sits alone inside a class-less wrapper div (the SPA/Tailwind norm)
    html = b"""<div id=grid>
      <div class="mt-4 flex"><article class=card><h3>One</h3></article></div>
      <div class="mt-4 flex"><article class=card><h3>Two</h3></article></div>
      <div class="mt-4 flex"><article class=card><h3>Three</h3></article></div>
    </div>"""
    doc = parse(html, content_type="text/html")
    top = doc.records()[0]
    assert (
        top.item_selector == "article.card" and top.count == 3
    )  # inner record, not the div wrapper


def test_json_dotted_path_and_skeleton() -> None:
    doc = parse(
        b'{"data": {"results": [{"name": "Ann", "age": 30}, {"name": "Bo"}]}, "next": "c1"}',
        content_type="application/json",
    )
    assert doc.at("data.results[0].name") == "Ann"
    assert doc.at("data.results[1].name") == "Bo"
    assert doc.at("next") == "c1"
    assert doc.at("data.missing") is None and doc.at("data.results[9]") is None
    sk = doc.json_skeleton()
    assert (
        "results: [2]" in sk and "name: string" in sk and "age: number" in sk
    )  # merged element shape


def test_skeleton_marks_records_and_interactive() -> None:
    html = (
        b"<html><body>"
        b"<div onclick='x()' class='card'>clickme</div>"  # a non-obvious control (div)
        b"<ul class=list>"
        + b"".join(b"<li class=item><span class=t>x</span></li>" for _ in range(4))
        + b"</ul></body></html>"
    )
    doc = parse(html, content_type="text/html")
    sk = doc.skeleton()
    assert "← RECORD LIST" in sk and 'select_all("li.item")' in sk  # dataset flagged in place
    assert "← clickable" in sk  # the onclick div flagged
    # a native <a>/<button> is obvious and must NOT be marked clickable
    assert "a  ← clickable" not in doc.skeleton()


def test_skeleton_drop_chrome_omits_nav() -> None:
    html = b"<html><body><nav><a href=/x>menu</a></nav><main><p>content</p></main></body></html>"
    doc = parse(html, content_type="text/html")
    assert "nav" not in doc.skeleton(drop_chrome=True)
    assert "nav" in doc.skeleton(drop_chrome=False)


def test_tables_degenerate_no_cells_does_not_crash() -> None:
    # a <table> with rows but no cells: previously transpose=True crashed (empty matrix unpack)
    doc = parse(b"<table><tr></tr><tr></tr></table>", content_type="text/html")
    assert doc.tables() == []
    assert doc.tables(transpose=True) == []
    # a header-only table yields no data rows, not junk
    doc2 = parse(b"<table><tr><th>A</th><th>B</th></tr></table>", content_type="text/html")
    assert doc2.tables() == []


def test_json_reads_are_safe_on_non_json_documents() -> None:
    # symmetry with markup reads being safe on JSON: JSON reads no-op on non-JSON (don't raise)
    html = parse(b"<html><body><p>hi</p></body></html>", content_type="text/html")
    assert html.at("data.x") is None
    assert html.json_skeleton() == ""
    assert html.json_leaves() == []
    # and still work on a real JSON document
    j = parse(b'{"a": {"b": 5}}', content_type="application/json")
    assert j.at("a.b") == 5 and "a: {" in j.json_skeleton()


def test_mislabelled_charset_does_not_crash_text() -> None:
    # a bogus/unsupported charset must fall back to utf-8, not raise LookupError on .text
    doc = parse("<html>café</html>".encode("utf-8"), content_type="text/html; charset=bogus")
    assert doc.encoding == "utf-8"
    assert "café" in doc.text
    # a real declared charset is still honoured
    win = parse("naïve".encode("windows-1252"), content_type="text/plain; charset=windows-1252")
    assert win.encoding == "windows-1252" and win.text == "naïve"
    # a meta charset with a typo also falls back
    assert parse(b"<meta charset=notacodec><p>x</p>", content_type="text/html").encoding == "utf-8"


def test_jsonld_preserves_significant_whitespace_in_values() -> None:
    # JSON-LD string values with multiple spaces must survive (whitespace collapse would corrupt them)
    html = b'<html><head><script type="application/ld+json">{"name": "ACME  Corp", "sku": "A  1"}</script></head></html>'
    md = parse(html, content_type="text/html").metadata()
    assert md.ld_json == [{"name": "ACME  Corp", "sku": "A  1"}]


def test_sniff_text_with_multibyte_char_at_512_boundary() -> None:
    # a UTF-8 char split at the 512-byte sniff cut must NOT misclassify valid text as binary
    body = ("x" * 511 + "é" + " tail").encode("utf-8")  # 'é' (2 bytes) straddles byte 512
    assert parse(body).kind == "text"
    # a genuine binary blob (invalid utf-8 mid-stream) is still binary
    assert parse(b"\x00\x01\xff\xfe" * 200).kind == "binary"
