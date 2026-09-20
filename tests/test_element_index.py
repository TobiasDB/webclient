"""Phase 3 -- the indexed element table primitive: durable (class-free) selectors, the
interactive `controls()` / content `content_elements()` tables, repetition marking, and the
numbered `element_table()` rendering. All static (a parsed page, no browser)."""

from webclient.core.document import Document
from webclient.core.document.element_index import durable_selector, index_elements
from webclient.dom import parse_html

PAGE = """
<html><body>
  <header><nav><a href="/home">Home</a></nav></header>
  <main>
    <form>
      <input name="q" type="search" aria-label="Search products">
      <button id="go">Search</button>
      <button class="btn primary-action">Filter</button>
    </form>
    <ul class="results">
      <li class="card"><h3 class="title">Aeropress</h3><span class="price">$39</span></li>
      <li class="card"><h3 class="title">Grinder</h3><span class="price">$59</span></li>
      <li class="card"><h3 class="title">Kettle</h3><span class="price">$79</span></li>
    </ul>
    <a href="/page/2" aria-label="Next page" class="css-1a2b3c">Next</a>
  </main>
</body></html>
"""


def _doc() -> Document:
    return Document(url="http://x/", content=PAGE.encode(), kind="html", status_code=200)


def _by_selector(root, css):
    return root.cssselect(css)[0]


def test_durable_selector_prefers_id_name_aria_then_class_then_structure():
    root = parse_html(PAGE)
    # 1. a word-like id wins
    assert durable_selector(_by_selector(root, "#go")) == "#go"
    # 2. a form-field name
    assert durable_selector(_by_selector(root, "input")) == 'input[name="q"]'
    # 3. aria-label beats a (hashed) class
    assert durable_selector(_by_selector(root, "a[href='/page/2']")) == 'a[aria-label="Next page"]'
    # 4. a single stable semantic class (the hashed/utility ones are dropped, longest kept)
    assert durable_selector(_by_selector(root, ".primary-action")) == "button.primary-action"
    # 5. no id/name/aria/class -> a structural nth-of-type path
    price = root.cssselect(".price")[1]  # the 2nd price -- but it HAS a class, so:
    assert durable_selector(price) == "span.price"


def test_durable_selector_scopes_a_field_within_a_record():
    root = parse_html(PAGE)
    record = root.cssselect("li.card")[0]
    title = record.cssselect("h3")[0]
    # within=record -> a relative selector (stops at the record subtree), so it evaluates per row
    sel = durable_selector(title, within=record)
    assert sel == "h3.title"  # class-based here; the point is it does not climb above the record


def test_controls_lists_interactive_elements_with_durable_selectors():
    ctrls = _doc().controls()
    roles = {c.role for c in ctrls}
    assert "textbox" in roles and "button" in roles and "link" in roles
    # every control has an index and a durable, class-free-ish selector
    assert [c.index for c in ctrls] == list(range(1, len(ctrls) + 1))
    go = next(c for c in ctrls if c.selector == "#go")
    assert go.role == "button" and go.name == "Search"
    search = next(c for c in ctrls if c.selector == 'input[name="q"]')
    assert search.role == "textbox" and search.name == "Search products"  # from aria-label


def test_content_elements_mark_repeated_records():
    rows = _doc().content_elements()
    # the record list has 3 identical <li> -> the titles/prices inside are marked repeats>=3
    repeated = [r for r in rows if r.repeats >= 3]
    assert any(r.name == "Aeropress" for r in repeated)
    assert any(r.name.startswith("$") for r in repeated)
    # a one-off element (a nav link) is not marked repeated
    assert all(r.repeats == 1 for r in rows if r.name == "Home")


def test_element_table_renders_numbered_lines():
    table = _doc().element_table()  # interactive by default
    lines = table.splitlines()
    assert lines[0].startswith("1  ")
    assert any('button "Search"' in ln for ln in lines)
    # content table marks repeats
    content = _doc().element_table(interactive=False)
    assert "(repeats ×3)" in content


def test_index_elements_is_bounded():
    root = parse_html("<html><body>" + "<button>b</button>" * 500 + "</body></html>")
    assert len(index_elements(root, kind="interactive", limit=50)) == 50
