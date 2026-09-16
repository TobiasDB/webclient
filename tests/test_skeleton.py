"""Extensive tests for the token-lean DOM skeleton (doc.skeleton()).

The skeleton is novel and must be robust: the sibling merge is lossless (a
differently-shaped sibling is never collapsed away), it survives SPA / framework
markup, deep nesting, huge pages and malformed input, and it annotates
server-initial vs client-injected (XHR/JS) content.
"""

import pytest

from webclient import Document


def sk(html: bytes, **kw) -> str:
    return Document(content=html, status_code=200).skeleton(**kw)


def lines(html: bytes, **kw) -> list[str]:
    return [l for l in sk(html, **kw).splitlines() if not l.startswith("#")]


# -- basics -------------------------------------------------------------------


def test_basic_outline_keeps_ids_and_classes():
    out = sk(b'<html><body><main id="m" class="a b"><p class="x">hi</p></main></body></html>')
    assert '<main id="m" class="a b">' in out
    assert '<p class="x">' in out and '"hi"' in out


def test_render_skeleton_equals_method():
    d = Document(content=b"<html><body><p>x</p></body></html>", status_code=200)
    assert d.render("skeleton") == d.skeleton()


def test_legend_present_and_toggleable():
    assert sk(b"<html><body><p>x</p></body></html>").splitlines()[0].startswith("# skeleton:")
    assert not sk(b"<html><body><p>x</p></body></html>", legend=False).startswith("#")


# -- MERGE SAFETY (the dangerous part) ----------------------------------------


def test_siblings_are_not_collapsed_by_default():
    # the default is a faithful outline: every sibling on its own line, no ×N.
    html = b"<html><body><ul>" + b'<li class="i">x</li>' * 4 + b"</ul></body></html>"
    out = sk(html)
    assert "×" not in out
    assert out.count('<li class="i">') == 4  # all four shown, not merged


def test_identical_siblings_merge():
    html = b"<html><body><ul>" + b'<li class="i"><span class="t">x</span></li>' * 5 + b"</ul></body></html>"
    out = sk(html, collapse=True)
    assert '<li class="i"> ×5' in out
    assert out.count('<li class="i">') == 1  # collapsed to a single representative line


def test_structurally_different_sibling_is_NOT_merged_away():
    # two plain items + one with an extra .badge: the odd one must be shown in full.
    html = (
        b"<html><body><ul>"
        b'<li class="i"><span class="t">a</span></li>'
        b'<li class="i"><span class="t">b</span></li>'
        b'<li class="i"><span class="t">c</span><span class="badge">SALE</span></li>'
        b"</ul></body></html>"
    )
    out = sk(html, collapse=True)
    assert '<li class="i"> ×2' in out       # the two identical ones merged
    assert 'class="badge"' in out     # the odd sibling's extra field survives
    # the merged run and the odd one are separate lines
    assert out.count('<li class="i">') == 2


def test_different_class_siblings_do_not_merge():
    html = b'<html><body><div class="a">1</div><div class="b">2</div></body></html>'
    out = sk(html, legend=False)
    assert '<div class="a">' in out and '<div class="b">' in out and "×" not in out


def test_ids_are_unique_so_siblings_never_merge():
    html = b'<html><body><section id="one">a</section><section id="two">b</section></body></html>'
    out = sk(html, legend=False)
    assert '<section id="one">' in out and '<section id="two">' in out and "×" not in out


def test_merge_is_order_sensitive_runs_only():
    # a, a, b, a  -> the two leading a's merge; the trailing a is its own line.
    html = b'<html><body><i class="a">1</i><i class="a">2</i><i class="b">3</i><i class="a">4</i></body></html>'
    out = sk(html, collapse=True)
    assert '<i class="a"> ×2' in out and '<i class="b">' in out
    assert out.count('<i class="a">') == 2  # the run of 2, plus the lone trailing one


# -- bloat removal ------------------------------------------------------------


def test_noise_tags_removed():
    html = (
        b"<html><head><meta charset=utf-8><link rel=x><title>T</title></head>"
        b"<body><script>evil()</script><style>.x{}</style>"
        b"<noscript>n</noscript><svg><path d=M0/></svg>"
        b"<p>real</p></body></html>"
    )
    out = sk(html)
    for noise in ("script", "style", "noscript", "svg", "path", "meta", "link", "head"):
        assert noise not in out
    assert "p" in out and '"real"' in out


def test_html_comments_and_pis_are_skipped():
    html = b"<html><body><!-- a comment --><p>x</p></body></html>"
    out = sk(html)
    assert "comment" not in out and "p" in out


# -- attributes ---------------------------------------------------------------


def test_selector_relevant_attributes_surface():
    html = (
        b"<html><body>"
        b'<input type="email" name="e" placeholder="Email" aria-label="Email addr">'
        b'<a href="/x" role="button">go</a>'
        b'<img src="/p.png" alt="pic">'
        b'<div data-testid="cart">c</div>'
        b"</body></html>"
    )
    out = sk(html)
    assert '<input type="email" name="e" placeholder="Email" aria-label="Email addr">' in out
    assert '<a role="button" href>' in out
    assert '<img alt="pic" src>' in out
    assert '<div data-testid="cart">' in out


def test_class_soup_is_capped():
    classes = " ".join(f"c{i}" for i in range(20))
    html = f'<html><body><div class="{classes}">x</div></body></html>'.encode()
    out = sk(html)
    assert "…+12" in out  # 20 classes - 8 shown = 12 hidden
    assert "c9" not in out  # beyond the cap


def test_attribute_values_are_clipped_and_normalised():
    html = b'<html><body><div role="a   b" title="' + b"z" * 100 + b'">x</div></body></html>'
    out = sk(html)
    assert 'role="a b"' in out  # whitespace collapsed
    assert "z" * 24 in out and "z" * 25 not in out  # clipped to 24


# -- text hints ---------------------------------------------------------------


def test_text_hint_on_leaves_only_and_truncated():
    html = b"<html><body><p>" + b"word " * 40 + b"</p><div><span>child</span></div></body></html>"
    out = sk(html)
    p_line = next(l for l in out.splitlines() if l.strip().startswith("<p"))
    assert p_line.endswith('…"') and "word" in p_line  # leaf p has a truncated hint
    div_line = next(l for l in out.splitlines() if l.strip().startswith("<div"))
    assert '"' not in div_line  # a node with element children gets no text hint


def test_text_hint_normalises_whitespace_and_quotes():
    html = b"<html><body><p>  a\n\t  b  </p></body></html>"
    out = sk(html)
    assert '"a b"' in out


# -- bounds -------------------------------------------------------------------


def test_max_siblings_bounds_a_huge_flat_list():
    # 5000 DISTINCT-structure siblings (each a unique id, so no merge) must be capped.
    items = b"".join(f'<div id="d{i}">{i}</div>'.encode() for i in range(5000))
    out = sk(b"<html><body>" + items + b"</body></html>", max_siblings=50)
    assert "more)" in out
    body = [l for l in out.splitlines() if '<div id="d' in l]
    assert len(body) <= 51


def test_max_lines_bounds_output():
    items = b"".join(f'<div id="d{i}"><span id="s{i}">x</span></div>'.encode() for i in range(500))
    out = sk(b"<html><body>" + items + b"</body></html>", max_lines=30)
    assert "truncated" in out
    assert len([l for l in out.splitlines() if not l.startswith("#")]) <= 32


def test_deep_nesting_is_depth_bounded_and_does_not_crash():
    html = b"<html><body>" + b"<div>" * 500 + b"leaf" + b"</div>" * 500 + b"</body></html>"
    out = sk(html, max_depth=20)  # must not RecursionError
    assert "…" in out
    depths = [len(l) - len(l.lstrip()) for l in out.splitlines() if not l.startswith("#")]
    assert max(depths) <= (20 + 1) * 2  # indentation bounded by max_depth


# -- edge cases ---------------------------------------------------------------


@pytest.mark.parametrize("html", [b"", b"   ", b"<html></html>", b"<html><body></body></html>"])
def test_empty_or_trivial_pages_do_not_crash(html):
    out = Document(content=html, status_code=200).skeleton()
    assert isinstance(out, str)  # no exception


def test_malformed_html_recovers():
    out = sk(b"<html><body><div class=x><p>unclosed<ul><li>a</body>")
    assert '<div class="x">' in out  # lxml recover mode still produces a tree


def test_tables_render():
    html = (
        b"<html><body><table><thead><tr><th>H</th></tr></thead>"
        b"<tbody><tr><td>1</td></tr><tr><td>2</td></tr></tbody></table></body></html>"
    )
    out = sk(html)
    assert "table" in out and "tr" in out and "td" in out


def test_xml_document_skeletonises():
    xml = b'<?xml version="1.0"?><feed><entry><title>A</title></entry><entry><title>B</title></entry></feed>'
    d = Document(kind="xml", content=xml, status_code=200)
    out = d.skeleton(collapse=True)
    assert '<entry> ×2' in out  # two identical entries merge


# -- origin annotation (initial vs XHR/JS) ------------------------------------


def _spa(rendered: bytes, static: bytes, xhr_url: str | None = None) -> Document:
    d = Document(content=rendered, status_code=200)
    d._static_html = static
    if xhr_url:
        from webclient.core.reference import from_url
        from webclient.models import NetworkEvent

        e = NetworkEvent(resource_type="fetch")
        e.request = from_url(xhr_url)
        d._events = [e]
    return d


def test_injected_nodes_marked_xhr_with_api_header():
    d = _spa(
        rendered=b'<html><body><div id="app"><ul class="list"><li class="quote">Q1</li><li class="quote">Q2</li></ul></div></body></html>',
        static=b'<html><body><div id="app"></div></body></html>',
        xhr_url="https://x/api/quotes",
    )
    out = d.skeleton(collapse=True)
    assert "# XHR/fetch data APIs: https://x/api/quotes" in out
    assert '<div id="app">' in out and "[xhr]" not in _first_line_for(out, '<div id="app">')  # shell: initial
    assert "[xhr]" in _first_line_for(out, '<ul class="list">')  # injected
    assert '<li class="quote"> [xhr] ×2' in out


def test_injected_nodes_marked_js_without_xhr():
    d = _spa(
        rendered=b'<html><body><div id="app"><p class="c">injected</p></div></body></html>',
        static=b'<html><body><div id="app"></div></body></html>',
    )
    out = d.skeleton()
    assert "[js]" in _first_line_for(out, '<p class="c">')
    assert "XHR/fetch" not in out  # no xhr calls observed


def test_no_static_baseline_means_no_origin_marks():
    # a plain document (no _static_html) is never annotated.
    out = sk(b'<html><body><div class="x">a</div></body></html>')
    assert "[xhr]" not in out and "[js]" not in out


def test_annotate_origin_can_be_disabled():
    d = _spa(
        rendered=b'<html><body><p class="c">x</p></body></html>',
        static=b"<html><body></body></html>",
    )
    assert "[js]" not in d.skeleton(annotate_origin=False)


def _first_line_for(out: str, needle: str) -> str:
    return next(l for l in out.splitlines() if needle in l)


# -- json / xml skeletons -----------------------------------------------------


def test_json_skeleton_shows_paths_and_types():
    # a JSON/API document gets a shape outline (keys + types, arrays as [N] with the
    # element shape) so an LLM can write dotted-path queries against it.
    doc = Document(
        content=b'{"results":[{"id":1,"name":"Aeropress"},{"id":2,"name":"Grinder"}],"total":2}',
        kind="json",
        status_code=200,
    )
    out = doc.skeleton()
    assert "results: [2]" in out  # the array with its length
    assert "name: string" in out and "id: number" in out  # the element's shape
    assert "total: number" in out


def test_xml_skeleton_uses_the_dom_outline():
    # XML is parsed as a tree, so the DOM skeleton already outlines it.
    doc = Document(
        content=b"<catalog><item><name>Aeropress</name></item></catalog>",
        kind="xml",
        status_code=200,
    )
    out = doc.skeleton()
    assert "<item>" in out and "<name>" in out and '"Aeropress"' in out
