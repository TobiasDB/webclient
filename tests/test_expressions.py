"""P2 gate: the independent ``Expr`` recorder -- records anything, needs no
return types, refuses ``_``-names, JSON round-trips, validates on the wire."""

import pytest

from webclient import Reference, doc, field, from_plan, many, ref, reference
from webclient.query.expr import Expr
from webclient.query.plan import Plan, Step


def kinds(expr):
    return [(s.kind, s.name) for s in expr._plan.steps]


def test_roots_record_attribute_and_call_steps():
    expr = doc.select("a", index=1).attr("href")
    assert expr.is_lazy and expr._plan.root == "Document"
    assert kinds(expr) == [
        ("get", "select"),
        ("call", ""),
        ("get", "attr"),
        ("call", ""),
    ]
    call = expr._plan.steps[1]
    assert call.args[0].value == "a" and call.kwargs["index"].value == 1


def test_any_public_attribute_records_no_return_type_needed():
    assert kinds(ref.url) == [("get", "url")]
    assert kinds(ref.with_params(page="2")) == [("get", "with_params"), ("call", "")]
    assert kinds(ref.resolve().select("a").attr("href"))[-2:] == [
        ("get", "attr"),
        ("call", ""),
    ]


def test_reference_url_is_a_lazy_root_with_a_source():
    r = reference("https://e.com/s?q=1")
    assert isinstance(r, Expr) and r._plan.root == "Reference"
    assert r._plan.source["hostname"] == "e.com"
    assert r._plan.source["params"] == {"q": "1"}
    assert not isinstance(Reference(hostname="e.com"), Expr)  # keyword: eager


def test_comparisons_and_logic_record_as_op_steps():
    expr = (doc.text_content == "x") & ~(doc.attr("a") != "y")
    assert expr._plan.steps[-1].kind == "op" and expr._plan.steps[-1].name == "and"
    other = expr._plan.steps[-1].args[0].plan
    assert [s.name for s in other.steps if s.kind == "op"] == ["ne", "not"]


def test_python_coercion_is_refused_on_expressions():
    for thunk, msg in [
        (lambda: bool(doc.attr("x")), "truth value"),
        (lambda: [x for x in many], "iterator"),
        (lambda: len(many.select_all("a")), "length"),
        (lambda: doc.attr("x") and 1, "truth value"),
    ]:
        with pytest.raises(TypeError, match=msg):
            thunk()


def test_extract_and_functions_record_subplans():
    expr = many.extract(title=doc.select(".t").text_content, flag=field("title"))
    call = expr._plan.steps[1]
    assert call.kwargs["title"].plan.root == "Document"
    assert call.kwargs["flag"].plan.steps[0].name == "field"
    from webclient import is_empty

    assert is_empty(doc.attr("x"))._plan.steps[-1].kind == "fn"


def test_json_roundtrip_and_wire_validation():
    expr = ref.resolve().select_all(".card").extract(t=doc.text_content).project()
    wire = expr._plan.model_dump_json()
    back = from_plan(Plan.model_validate_json(wire))
    assert back._plan == expr._plan and back.is_lazy
    with pytest.raises(ValueError, match="unknown plan root"):
        from_plan({"root": "Client", "steps": []})


def test_private_names_are_refused_at_record_and_on_the_wire():
    """The __subclasses__ escape needs a dunder; Expr never records one, and a
    hand-built plan naming one fails validation (the whole safety model)."""
    with pytest.raises(AttributeError):
        doc._evil  # a private name is never recorded
    plan = Plan(root="Document", steps=[Step(kind="get", name="__class__")])
    with pytest.raises(ValueError, match="private name"):
        plan.validate_names()
    assert not any(
        s.name.startswith("_")
        for s in doc.select("a").attr("href").resolve()._plan.steps
    )


def test_describe_is_human_readable():
    text = reference("https://e.com/").resolve().select_all(".c")._plan.describe()
    assert text == "Reference(e.com).resolve().select_all('.c')"


def test_blob_roundtrips_and_rebuilds_the_expression():
    from webclient.query.expr import from_blob

    expr = ref.resolve().select_all(".card").extract(t=doc.text_content).project()
    blob = expr.to_blob()
    assert blob.startswith(("p0:", "p1:")) and " " not in blob
    back = from_blob(blob)
    assert back._plan == expr._plan  # exact rebuild
    assert back.explain() == expr.explain()  # and pretty-prints the same


def test_blob_is_accepted_by_from_plan_and_validated():
    # an LLM authoring path: a blob is a valid from_plan input, but still passes
    # through name validation (the wire safety boundary).
    good = doc.select("a").attr("href").to_blob()
    assert from_plan(good).is_lazy
    evil = Plan(root="Document", steps=[Step(kind="get", name="a")]).to_blob()
    # tamper: a hand-built plan naming a private op still fails on rebuild.
    bad = Plan(root="Document", steps=[Step(kind="get", name="__class__")]).to_blob()
    assert from_plan(evil).is_lazy  # a normal name is fine
    with pytest.raises(ValueError, match="private name"):
        from_plan(bad)


def test_corrupt_blob_is_rejected():
    from webclient.query.expr import from_blob

    with pytest.raises(ValueError, match="not a plan blob"):
        from_blob("nope")
    with pytest.raises(ValueError, match="corrupt plan blob"):
        from_blob("p1:!!!!")
