import pytest

from webclient import Expr, OnError, QueryPlan, col, lit, q


def test_records_chain_without_executing():
    expr = q.ref.fetch().select_all(".card")
    assert isinstance(expr, Expr)
    plan = expr.to_query()
    assert plan.root == "Reference"
    assert [s.get("name") for s in plan.steps if s.get("op") == "call"] == [
        "fetch", "select_all"]


def test_attribute_access_records_get():
    plan = q.node.select(".title").text.to_query()
    ops = [(s.get("op"), s.get("name")) for s in plan.steps]
    assert ("call", "select") in ops
    assert ("get", "text") in ops


def test_record_time_validation_catches_typos():
    with pytest.raises(AttributeError, match="selct"):
        q.node.selct(".title")
    with pytest.raises(AttributeError, match="no attribute"):
        q.ref.fetch().nonsense_method()


def test_valid_names_across_transitions():
    # select() -> Node, so .text and further .select validate
    q.doc.select(".a").select(".b").text
    q.ref.fetch().select_all(".card")     # fetch()->Document, select_all ok


def test_comparison_builds_boolean_expr():
    expr = q.node.select(".status").text == "Active"
    step = expr.to_query().steps[-1]
    assert step["binop"] == "eq" and step["value"] == "Active"


def test_map_filter_then_otherwise():
    plan = (
        q.ref.fetch().select_all(".card")
        .map(
            title=q.node.select(".title").text,
            active=q.node.select(".status").text == "Active",
        )
        .filter(col("active"))
        .then(link=col("title"))
        .otherwise(OnError.skip)
        .to_query()
    )
    ops = [s.get("op") for s in plan.steps]
    assert ops[-4:] == ["map", "filter", "then", "otherwise"]
    map_step = next(s for s in plan.steps if s.get("op") == "map")
    assert set(map_step["fields"]) == {"title", "active"}


def test_col_and_lit():
    assert col("x").to_query().steps == [{"op": "col", "name": "x"}]
    assert lit(5).to_query().steps == [{"op": "lit", "value": 5}]


def test_query_plan_roundtrips_via_json():
    expr = (
        q.ref.fetch().select_all(".card")
        .map(title=q.node.select(".title").text)
        .filter(col("title") != "")
    )
    plan = expr.to_query()
    dumped = plan.model_dump_json()
    restored = Expr.from_query(QueryPlan.model_validate_json(dumped))
    assert restored.to_query().steps == plan.steps
    assert restored.to_query().root == "Reference"


def test_explain_is_human_readable():
    text = q.ref.fetch().select_all(".card").map(t=q.node.text).explain()
    assert "root: Reference" in text
    assert ".fetch(...)" in text and "map(t)" in text


def test_dir_delegates_to_wrapped_class():
    assert "fetch" in dir(q.ref)
    assert "select" in dir(q.node)


def test_collect_defers_to_executor(monkeypatch):
    from webclient import WebClient
    with WebClient() as wc:
        with pytest.raises(NotImplementedError, match="M6"):
            q.ref.fetch().collect(wc.ref("https://e.com"), client=wc)


def test_live_proxies_exist():
    assert q.live.fetch  # LiveDocument-rooted
    assert "click" in dir(q.live)
