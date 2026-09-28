"""``.alias(name)`` names an extract column from the chain itself -- a literal, or an
expression read off the element -- so a key/value table becomes a dict (``merge()``)."""

from webclient import WebClient
from webclient.interface import wq

TABLE = """<html><body>
<h1>The Book</h1><p class="desc">A fine read.</p>
<table><tr><th>UPC</th><td>abc123</td></tr><tr><th>Price</th><td>£9.99</td></tr><tr><th>Stock</th><td>In stock (3)</td></tr></table>
</body></html>"""


def test_alias_literal_names_a_positional_column(httpserver):
    httpserver.expect_request("/b").respond_with_data(TABLE, content_type="text/html")
    plan = wq.reference(httpserver.url_for("/b")).resolve().extract(wq.doc.select("h1").attr("text").alias("title")).project()
    assert plan.collect() == {"title": "The Book"}


def test_alias_expression_reads_the_name_off_the_element(httpserver):
    httpserver.expect_request("/b").respond_with_data(TABLE, content_type="text/html")
    plan = (
        wq.reference(httpserver.url_for("/b")).resolve()
        .select_all("table tr")
        .extract(wq.doc.select("td").attr("text").alias(wq.doc.select("th").attr("text")))
        .project()
    )
    assert plan.collect() == [{"UPC": "abc123"}, {"Price": "£9.99"}, {"Stock": "In stock (3)"}]


def test_merge_folds_the_rows_into_one_dict(httpserver):
    httpserver.expect_request("/b").respond_with_data(TABLE, content_type="text/html")
    info = (
        wq.reference(httpserver.url_for("/b")).resolve()
        .select_all("table tr")
        .extract(wq.doc.select("td").attr("text").alias(wq.doc.select("th").attr("text")))
        .merge()
    )
    assert info.collect() == {"UPC": "abc123", "Price": "£9.99", "Stock": "In stock (3)"}


def test_merge_nests_as_a_field_and_streams(httpserver):
    # the books shape: a detail page's description + its key/value table as one nested dict
    httpserver.expect_request("/b").respond_with_data(TABLE, content_type="text/html")
    plan = (
        wq.reference(httpserver.url_for("/b")).resolve()
        .extract(
            description=wq.doc.select("p.desc").attr("text"),
            info=wq.doc.select_all("table tr").extract(wq.doc.select("td").attr("text").alias(wq.doc.select("th").attr("text"))).merge(),
        )
        .project()
    )
    with WebClient() as wc:
        row = wc.execute(plan)
    assert row == {"description": "A fine read.", "info": {"UPC": "abc123", "Price": "£9.99", "Stock": "In stock (3)"}}


def test_positional_column_without_alias_is_rejected(httpserver):
    import pytest

    from webclient.kernel.errors import WebException

    httpserver.expect_request("/b").respond_with_data(TABLE, content_type="text/html")
    plan = wq.reference(httpserver.url_for("/b")).resolve().extract(wq.doc.select("h1").attr("text")).project()
    with pytest.raises(WebException) as exc:
        plan.collect()
    assert exc.value.error.code == "plan.invalid"


def test_alias_round_trips_through_the_blob(httpserver):
    from webclient import from_blob

    httpserver.expect_request("/b").respond_with_data(TABLE, content_type="text/html")
    plan = wq.reference(httpserver.url_for("/b")).resolve().select_all("tr").extract(wq.doc.select("td").attr("text").alias(wq.doc.select("th").attr("text"))).merge()
    with WebClient() as wc:
        assert from_blob(plan._plan.to_blob(), wc).collect() == {"UPC": "abc123", "Price": "£9.99", "Stock": "In stock (3)"}


def test_alias_from_a_previous_column_consumes_it(httpserver):
    # name=th, value=td.alias(field("name")): the value is keyed by its row's name; `name` is used up
    httpserver.expect_request("/b").respond_with_data(TABLE, content_type="text/html")
    plan = (
        wq.reference(httpserver.url_for("/b")).resolve()
        .select_all("table tr")
        .extract(name=wq.doc.select("th").attr("text"), value=wq.doc.select("td").attr("text").alias(wq.doc.field("name")))
        .merge()
    )
    assert plan.collect() == {"UPC": "abc123", "Price": "£9.99", "Stock": "In stock (3)"}


def test_a_named_column_can_carry_an_alias(httpserver):
    httpserver.expect_request("/b").respond_with_data(TABLE, content_type="text/html")
    plan = wq.reference(httpserver.url_for("/b")).resolve().extract(value=wq.doc.select("h1").attr("text").alias("title")).project()
    assert plan.collect() == {"title": "The Book"}


def test_alias_from_a_column_streams_and_round_trips(httpserver):
    from webclient import from_blob

    httpserver.expect_request("/b").respond_with_data(TABLE, content_type="text/html")
    plan = (
        wq.reference(httpserver.url_for("/b")).resolve()
        .select_all("table tr")
        .extract(name=wq.doc.select("th").attr("text"), value=wq.doc.select("td").attr("text").alias(wq.doc.field("name")))
        .project()
    )
    with WebClient() as wc:
        again = from_blob(plan._plan.to_blob(), wc)
        assert list(again.stream()) == [{"UPC": "abc123"}, {"Price": "£9.99"}, {"Stock": "In stock (3)"}]


def test_project_flattens_chosen_nested_columns(httpserver):
    from webclient.query.collection import flatten_row

    row = {"title": "T", "detail": {"description": "D", "info": {"UPC": "u", "Tax": "0"}}, "tags": [{"a": 1}]}
    assert flatten_row(row, ["detail"]) == {"title": "T", "detail.description": "D", "detail.info": {"UPC": "u", "Tax": "0"}, "tags": [{"a": 1}]}
    assert flatten_row(row, True) == {"title": "T", "detail.description": "D", "detail.info.UPC": "u", "detail.info.Tax": "0", "tags": [{"a": 1}]}
    assert flatten_row(row, ["detail.info"]) == {"title": "T", "detail": {"description": "D", "info.UPC": "u", "info.Tax": "0"}, "tags": [{"a": 1}]}
    assert flatten_row(row, ["detail"], sep="_")["detail_description"] == "D"
    # in a plan, eager and streamed
    httpserver.expect_request("/b").respond_with_data(TABLE, content_type="text/html")
    d = wq.doc
    plan = (
        wq.reference(httpserver.url_for("/b")).resolve().select_all("body")
        .extract(title=d.select("h1").attr("text"), info=d.select_all("table tr").extract(name=d.select("th").attr("text"), value=d.select("td").attr("text").alias(d.field("name"))).merge())
        .project(flatten=["info"])
    )
    with WebClient() as wc:
        assert wc.execute(plan) == [{"title": "The Book", "info.UPC": "abc123", "info.Price": "£9.99", "info.Stock": "In stock (3)"}]
        assert list(plan.stream(wc)) == [{"title": "The Book", "info.UPC": "abc123", "info.Price": "£9.99", "info.Stock": "In stock (3)"}]


def test_document_project_flattens(httpserver):
    httpserver.expect_request("/b").respond_with_data(TABLE, content_type="text/html")
    d = wq.doc
    plan = wq.reference(httpserver.url_for("/b")).resolve().extract(
        title=d.select("h1").attr("text"),
        info=d.select_all("table tr").extract(name=d.select("th").attr("text"), value=d.select("td").attr("text").alias(d.field("name"))).merge(),
    ).project(flatten=True, sep="_")
    assert plan.collect() == {"title": "The Book", "info_UPC": "abc123", "info_Price": "£9.99", "info_Stock": "In stock (3)"}
