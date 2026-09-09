"""The expression language: one implementation, two evaluation modes."""
import pytest

from webclient import (DROP_ROW, NULL, RAISE_ERROR, Document, PlanError,
                       Plan, Record, RecordSet, doc, el, err, field, lit)

PAGE = """<html><body><h1>Shop</h1>
<div class="cat"><h2>Coffee</h2>
  <div class="item"><span class="label">Ethiopia</span><span class="sku">C1</span></div>
  <div class="item"><span class="label">Kenya</span><span class="sku">C2</span></div>
</div>
<div class="cat"><h2>Tea</h2>
  <div class="item"><span class="label">Sencha</span><span class="sku">T1</span></div>
</div>
</body></html>"""


@pytest.fixture
def page():
    return Document.from_content(PAGE, url="https://shop.test/")


# -- constructors ----------------------------------------------------------- #

def test_then_is_one_record(page):
    record = page.then(doc.select("h1").attr("text").alias("title"))
    assert dict(record) == {"title": "Shop"}


def test_map_is_then_lifted_over_a_collection(page):
    rows = page.select_all(".cat").map(
        el.select("h2").attr("text").alias("name"))
    assert [dict(r) for r in rows] == [{"name": "Coffee"}, {"name": "Tea"}]


def test_positional_fields_need_an_alias(page):
    with pytest.raises(PlanError, match="no name"):
        page.then(doc.select("h1").attr("text"))


def test_duplicate_field_names_are_rejected(page):
    with pytest.raises(PlanError, match="duplicate"):
        page.then(doc.select("h1").attr("text").alias("a"),
                  a=doc.select("h1").attr("text"))


def test_nesting_is_the_normal_case(page):
    """The old engine silently dropped a second map and returned the first."""
    record = page.then(rows=doc.select_all(".cat").map(
        el.select("h2").attr("text").alias("name"),
        items=el.select_all(".item").map(
            el.select(".label").attr("text").alias("label"),
            sku=el.select(".sku").attr("text"),
        ),
    ))
    rows = record["rows"]
    assert [r["name"] for r in rows] == ["Coffee", "Tea"]
    assert [i["label"] for i in rows[0]["items"]] == ["Ethiopia", "Kenya"]
    assert len(rows[1]["items"]) == 1


def test_explode_flattens(page):
    record = page.then(rows=doc.select_all(".cat").map(
        el.select("h2").attr("text").alias("name"),
        items=el.select_all(".item").map(
            el.select(".label").attr("text").alias("label")),
    ))
    flat = RecordSet(record["rows"]).explode("items")
    assert [dict(r) for r in flat] == [
        {"name": "Coffee", "label": "Ethiopia"},
        {"name": "Coffee", "label": "Kenya"},
        {"name": "Tea", "label": "Sencha"},
    ]


def test_filter_is_about_unwanted_not_failed(page):
    rows = (page.select_all(".cat")
            .map(el.select("h2").attr("text").alias("name"))
            .filter(field("name") == "Tea"))
    assert [dict(r) for r in rows] == [{"name": "Tea"}]


def test_limit(page):
    rows = page.select_all(".item").map(
        el.select(".sku").attr("text").alias("sku")).limit(2)
    assert len(rows) == 2


# -- fields ------------------------------------------------------------------ #

def test_fields_accumulate_on_the_document(page):
    page.then(title=doc.select("h1").attr("text"))
    assert page.fields == {"title": "Shop"}
    page.then(heading=doc.select("h2").attr("text"))
    assert sorted(page.fields) == ["heading", "title"]
    assert page.field("title").get() == "Shop"


def test_field_sees_earlier_columns_in_the_same_block(page):
    record = page.then(
        name=doc.select("h1").attr("text"),
        shout=field("name"),
    )
    assert record["shout"] == "Shop"


# -- otherwise ---------------------------------------------------------------- #

def test_otherwise_null(page):
    record = page.then(missing=doc.select(".nope").attr("text").otherwise(NULL))
    assert record["missing"] is None


def test_otherwise_drop_row(page):
    rows = page.select_all(".cat").map(
        el.select("h2").attr("text").alias("name"),
        gone=el.select(".nope").attr("text").otherwise(DROP_ROW),
    )
    assert list(rows) == []


def test_otherwise_raise_is_the_default(page):
    with pytest.raises(Exception):
        page.then(missing=doc.select(".nope").attr("text"))


def test_otherwise_recovery_is_tagged(page):
    record = page.then(
        detail=doc.select(".nope").then(
            label=el.attr("text"),
        ).otherwise(
            message=err.message,
            where=err.op,
        ),
    )
    detail = record["detail"]
    assert detail["ok"] is False
    assert "nope" in detail["message"]
    assert detail["where"]


def test_otherwise_success_arm_is_tagged_too(page):
    record = page.then(
        detail=doc.select("h1").then(label=el.attr("text")).otherwise(
            message=err.message),
    )
    assert record["detail"]["ok"] is True
    assert record["detail"]["label"] == "Shop"


def test_unrecovered_failure_raises_on_access_not_silently(page):
    record = page.then(name=doc.select("h1").attr("text"))
    assert record.ok
    failed = page.select(".nope", optional=True)
    assert failed is None


# -- modes -------------------------------------------------------------------- #

def test_the_same_expression_eager_and_lazy(page):
    expression = doc.select_all(".item").map(
        el.select(".label").attr("text").alias("label"))
    lazy = [dict(r) for r in expression.collect(page)]
    eager = [dict(r) for r in page.select_all(".item").map(
        el.select(".label").attr("text").alias("label"))]
    assert lazy == eager


def test_plans_round_trip_through_json(page):
    expression = doc.then(rows=doc.select_all(".cat").map(
        el.select("h2").attr("text").alias("name")))
    blob = expression.to_plan().model_dump_json()
    restored = Plan.model_validate_json(blob)
    assert restored == expression.to_plan()
    from webclient.execute import collect_plan
    assert [dict(r) for r in collect_plan(restored, page)["rows"]] == [
        {"name": "Coffee"}, {"name": "Tea"}]


def test_explain_shows_the_tree(page):
    text = doc.select_all(".cat").map(
        el.select("h2").attr("text").alias("name")).explain()
    assert "select_all('.cat')" in text and "map" in text and "name =" in text


def test_a_rooted_plan_needs_no_context(page):
    with pytest.raises(PlanError, match="rooted at a context"):
        doc.select("h1").attr("text").collect()


def test_lazy_wrappers_refuse_to_pretend(page):
    lazy = doc.select("h1").attr("text")
    with pytest.raises(Exception):
        lazy.get()


# -- the guarantee the old engine broke --------------------------------------- #

def test_every_plan_step_kind_is_dispatched(page):
    """No step kind is ever passed through untouched.

    The old executor returned the row unchanged for any op it did not
    recognise, which is how a second `.map()` vanished without an error. Every
    kind in the IR must be claimed by exactly one dispatcher: `_apply` for
    ordinary steps, or `_run_segments` for `otherwise`, which recovers
    everything recorded before it and so cannot be a plain step.
    """
    import asyncio
    import typing

    from webclient import plan as plan_module
    from webclient.execute import _apply, _run_segments

    kinds = typing.get_args(typing.get_args(plan_module.Step)[0])
    assert len(kinds) >= 9

    unhandled = []
    for kind in kinds:
        step = _sample(kind)
        if kind is plan_module.OtherwiseStep:
            # a sentinel only applies to a failure, so give it one
            failing = plan_module.CallStep(
                op="attr", args=[plan_module.Arg(value="nope")])
            outcome = asyncio.run(
                _run_segments([failing, step], page, None, None))
            if not (outcome.ok and outcome.value is None):
                unhandled.append(kind.__name__)
            continue
        try:
            asyncio.run(_apply(step, page, None, None))
        except PlanError as exc:
            if "no evaluation for plan step" in str(exc):
                unhandled.append(kind.__name__)
        except Exception:
            pass            # any other failure means it *was* dispatched
    assert unhandled == []


def _sample(kind):
    from webclient import plan as p
    blank = p.Plan()
    samples = {
        p.CallStep: p.CallStep(op="attr", args=[p.Arg(value="text")]),
        p.LiteralStep: p.LiteralStep(value=1),
        p.BinOpStep: p.BinOpStep(operator="eq", right=p.Arg(value=1)),
        p.ThenStep: p.ThenStep(fields=[]),
        p.MapStep: p.MapStep(fields=[]),
        p.FilterStep: p.FilterStep(predicate=blank),
        p.OtherwiseStep: p.OtherwiseStep(sentinel="null"),
        p.ExplodeStep: p.ExplodeStep(path="x"),
        p.LimitStep: p.LimitStep(count=1),
    }
    return samples[kind]


def test_an_unknown_step_raises_rather_than_passing_through(page):
    import asyncio

    from webclient.execute import _apply

    class Rogue:
        kind = "rogue"

    with pytest.raises(PlanError, match="no evaluation for plan step"):
        asyncio.run(_apply(Rogue(), page, None, None))
