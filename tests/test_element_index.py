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


# -- record detection on the SPA/Tailwind shape (utility classes + per-record wrapper divs) --

WRAPPED = """
<html><body>
  <nav class="site-nav jsx-7428"><a href="/">Home</a></nav>
  <main>
    <div class="css-96fb2a0"><article class="product flex jsx-1585"><span class="mt-6 name">Aeropress</span><span class="price">$39</span></article></div>
    <div class="mt-6 css-cfa4a88"><article class="css-1d11bb5 product"><span class="name">Grinder</span><span class="price">$59</span></article></div>
    <div class="css-3960e1d jsx-8591"><article class="jsx-5204 mt-5 flex product"><span class="name flex">Kettle</span><span class="price">$79</span></article></div>
  </main>
</body></html>
"""


def test_utility_classes_do_not_split_a_record_group():
    # a stray Tailwind utility (mt-6 on one record, not the others) must NOT split the sibling
    # group -- _is_noise_class now strips utilities, so the signature is stable.
    from webclient.core.document.html import _is_noise_class
    assert _is_noise_class("mt-6") and _is_noise_class("flex") and _is_noise_class("text-center")
    assert not _is_noise_class("product") and not _is_noise_class("row") and not _is_noise_class("feed-item")


def test_record_options_sees_through_wrapper_divs_to_the_semantic_record():
    from webclient.core.document.element_index import record_options
    from webclient.core.document.html import tree

    doc = Document(url="http://x/", content=WRAPPED.encode(), kind="html", status_code=200)
    opts = record_options(tree(doc))
    assert opts, "a record region should be detected on the wrapped/utility page"
    # the top region is the SEMANTIC record (article.product), not a bare over-matching div
    assert opts[0].selector == "article.product" and opts[0].repeats == 3


def test_wrapped_records_extract_correctly_through_the_index():
    # end to end: the detected record selector + a per-row field selector extract the right values
    from webclient.core.document.element_index import record_options
    from webclient.core.document.html import tree
    from webclient.interface import wq

    doc = Document(url="http://x/", content=WRAPPED.encode(), kind="html", status_code=200)
    rec = record_options(tree(doc))[0].selector  # "article.product"
    rows = wq.doc.select_all(rec).extract(n=wq.doc.select("span.name").attr("text")).project().collect(doc)
    assert [r["n"] for r in rows] == ["Aeropress", "Grinder", "Kettle"]
