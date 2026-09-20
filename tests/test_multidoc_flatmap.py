"""Phase 3a (pagination foundation): a multi-document plan flat-maps. When a Collection of
Documents is followed by a per-element ``select_all(...).extract(...).project()``, the executor
fans the ELEMENT prefix out per document, concatenates the per-document record Collections into
ONE flat Collection, then runs the shaping once -- so ``collect()`` returns flat rows (not a
nested list of per-document lists) and ``stream()`` yields them too (it used to raise). This is
general (any multi-document plan), not pagination-specific."""

from webclient.core.document import Document
from webclient.interface import wq
from webclient.query.collection import Collection

P1 = b'<html><body><ul><li class="r"><span class="n">A</span></li>' \
     b'<li class="r"><span class="n">B</span></li></ul></body></html>'
P2 = b'<html><body><ul><li class="r"><span class="n">C</span></li></ul></body></html>'


def _pages() -> Collection:
    return Collection([
        Document(url="http://x/1", content=P1, kind="html", status_code=200),
        Document(url="http://x/2", content=P2, kind="html", status_code=200),
    ])


def test_multidoc_select_all_extract_project_is_flat_on_collect():
    rows = wq.doc.select_all("li.r").extract(n=wq.doc.select(".n").attr("text")).project().collect(_pages())
    assert rows == [{"n": "A"}, {"n": "B"}, {"n": "C"}]  # flat across BOTH documents, not nested


def test_multidoc_stream_yields_the_same_flat_rows():
    # stream() used to raise AttributeError on a multi-document plan; now it streams flat rows.
    rows = list(wq.doc.select_all("li.r").extract(n=wq.doc.select(".n").attr("text")).project().stream(_pages()))
    assert rows == [{"n": "A"}, {"n": "B"}, {"n": "C"}]


def test_multidoc_stream_matches_collect():
    expr = wq.doc.select_all("li.r").extract(n=wq.doc.select(".n").attr("text")).project()
    assert list(expr.stream(_pages())) == expr.collect(_pages())


def test_multidoc_scalar_per_element_stays_a_flat_list():
    # a per-element SINGLE select + scalar attr fans out to a flat list of scalars (unchanged)
    assert wq.doc.select(".n").attr("text").collect(_pages()) == ["A", "C"]


def test_multidoc_filter_and_limit_apply_across_the_merged_collection():
    # limit(n) means n ROWS overall (across all documents), not n per document
    rows = wq.doc.select_all("li.r").extract(n=wq.doc.select(".n").attr("text")).project().collect(_pages())
    limited = wq.doc.select_all("li.r").limit(2).extract(n=wq.doc.select(".n").attr("text")).project().collect(_pages())
    assert len(rows) == 3 and limited == [{"n": "A"}, {"n": "B"}]  # 2 overall (both from page 1)
