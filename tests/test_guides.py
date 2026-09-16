"""The lazy-query guide: its op reference is generated live from the document /
collection surfaces' docstrings, so it can't drift from what the ops do."""

from webclient.guides import lazy_query_guide


def test_op_reference_is_generated_from_live_docstrings():
    guide = lazy_query_guide()
    assert "<!-- OP-REFERENCE -->" not in guide  # the marker was filled in

    # each op's line is its actual docstring's first sentence -> verbatim in the guide
    assert "- `.select` — The first element matching a CSS or XPath" in guide
    assert "- `.attr` — The one element accessor" in guide
    assert "- `.regex` — The first match of" in guide  # the regex backing op
    # collection-shaping ops come from Collection's docstrings
    assert "- `.extract`" in guide and "- `.filter`" in guide and "- `.project`" in guide


def test_guide_is_document_rooted_not_ref_resolve():
    # queries are the document-level extraction; the caller owns fetch/resolve, so the
    # guide teaches wq.doc and never wq.ref.resolve() as a starting point.
    guide = lazy_query_guide()
    assert "wq.doc.select_all" in guide
    assert "wq.ref.resolve()" not in guide


def test_new_document_op_would_appear_in_the_guide():
    # a regression guard for "generated from docstrings": if a query op is added to the
    # allow-list, its docstring must surface. `text_content` is a property op, proving
    # props are picked up too.
    assert "- `.text_content` — The element's visible text" in lazy_query_guide()
