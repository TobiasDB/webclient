"""Record-region detection: find the dataset's repeating structure (pure, no browser)."""

from lxml import html as _lh

from webclient.dom.records import RecordRegion, find_record_regions


def _tree(markup: str):
    return _lh.fromstring(markup)


def test_finds_the_repeating_record_list_and_suggests_a_selector():
    tree = _tree(
        '<html><body><main><ul class="news">'
        + "".join(f'<li class="item"><h3>T{i}</h3><time>d{i}</time><a href="/{i}">go</a></li>'
                  for i in range(6))
        + "</ul></main></body></html>"
    )
    regions = find_record_regions(tree)
    assert regions and regions[0].count == 6
    assert regions[0].item_selector == "li.item"


def test_a_rich_record_list_outranks_a_short_nav_menu():
    # a nav of 3 bare links vs a content list of 5 rich cards -> the cards win.
    tree = _tree(
        '<html><body>'
        '<nav><ul>'
        '<li><a href="/a">A</a></li><li><a href="/b">B</a></li><li><a href="/c">C</a></li>'
        '</ul></nav>'
        '<main><div class="grid">'
        + "".join(f'<div class="card"><h3>P{i}</h3><p>desc {i}</p><span class="price">${i}</span></div>'
                  for i in range(5))
        + "</div></main></body></html>"
    )
    top = find_record_regions(tree)[0]
    assert top.item_selector == "div.card" and top.count == 5


def test_no_region_when_nothing_repeats_enough():
    tree = _tree("<html><body><main><p>one</p><h1>title</h1></main></body></html>")
    assert find_record_regions(tree, min_items=3) == []


def test_bare_tag_selector_when_no_shared_class():
    tree = _tree("<html><body><ul>" + "<li>x</li>" * 4 + "</ul></body></html>")
    regions = find_record_regions(tree)
    assert regions and regions[0].item_selector == "li" and regions[0].count == 4


def test_differently_shaped_siblings_are_not_one_group():
    # an item with an extra <span> is a different shape -> it is not counted in the group.
    tree = _tree(
        '<html><body><ul class="l">'
        '<li class="i"><a href="/1">a</a></li>'
        '<li class="i"><a href="/2">b</a></li>'
        '<li class="i"><a href="/3">c</a></li>'
        '<li class="i"><a href="/4">d</a><span class="badge">new</span></li>'
        "</ul></body></html>"
    )
    top = find_record_regions(tree)[0]
    assert top.count == 3  # the 3 identical ones, not the badged fourth


def test_result_is_a_model():
    r = RecordRegion(item_selector="li.item", count=5, score=7.5)
    assert RecordRegion.model_validate(r.model_dump()) == r


def test_skeleton_marks_the_record_region_with_a_select_all():
    # the integration: the skeleton flags the dataset container in place.
    from webclient.core.document import Document

    html = (
        b'<html><body><main><ul class="news">'
        + b"".join(
            f'<li class="item"><h3>T{i}</h3><time>d{i}</time><a href="/{i}">go</a></li>'.encode()
            for i in range(6)
        )
        + b"</ul></main></body></html>"
    )
    sk = Document(content=html, kind="html", status_code=200).skeleton()
    assert "← RECORD LIST · 6 items" in sk
    assert 'select_all("li.item")' in sk


def test_drawings_never_become_records():
    """An SVG diagram with four repeated <g> groups (the website's home) must not outrank the
    real product grid: geometry repeats, but it is not data."""
    from webclient.dom.parse import parse_html
    from webclient.dom.records import find_record_regions

    html = """<html><body>
    <svg viewBox="0 0 100 100">""" + "".join(f'<g><rect x="{i}" /><text>r{i}</text></g>' for i in range(6)) + """</svg>
    <main>""" + "".join(f'<div class="card"><h2 class="title">P{i}</h2><span class="price">${i}</span></div>' for i in range(3)) + """</main>
    </body></html>"""
    regions = find_record_regions(parse_html(html), min_items=3)
    assert regions and regions[0].item_selector == "div.card"
    assert all("g" != r.item_selector for r in regions)


def test_item_selector_prefers_the_first_listed_class():
    """``class="card rounded-lg border p-4"``: the author's semantic hook comes first; utility
    words that survive the noise filter (``border``) must not win on length."""
    from webclient.dom.parse import parse_html
    from webclient.dom.records import find_record_regions

    html = "<html><body><main>" + "".join(
        f'<div class="card rounded-lg border p-4"><h2>P{i}</h2><span>${i}</span></div>' for i in range(3)) + "</main></body></html>"
    assert find_record_regions(parse_html(html), min_items=3)[0].item_selector == "div.card"
