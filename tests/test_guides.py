"""The web-query guide: its op reference is generated live from the document /
collection surfaces' docstrings, it teaches schema -> query, and it carries none of
the "lazy / doesn't run" framing (the author just writes a query)."""

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


def test_text_content_is_omitted_to_avoid_ambiguity():
    # attr("text") is the single way to read text; text_content is deliberately not in
    # the op reference so the author is never faced with two ways to do one thing.
    guide = lazy_query_guide()
    assert "text_content" not in guide
    assert '.attr("text")' in guide


def test_guide_is_document_rooted_and_free_of_meta_framing():
    guide = lazy_query_guide().lower()
    assert "wq.doc.select_all" in guide
    assert "wq.ref.resolve()" not in guide
    # no "lazy / records / doesn't run" framing -- it's just how to write a query
    for word in ("lazy", "records, it does not run", "nothing evaluates"):
        assert word not in guide


def test_guide_shows_schema_to_query_worked_examples():
    guide = lazy_query_guide()
    assert "Turning a schema into a query" in guide
    assert "Worked examples" in guide
    # a worked example pairs a skeleton + schema with the resulting query
    assert "Skeleton:" in guide and "Schema:" in guide and "Query:" in guide
    assert ".select_all(" in guide and ".project()" in guide
