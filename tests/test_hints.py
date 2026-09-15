"""Runtime return-type resolution (webclient.query.hints): the recorder reads a
backing op's own overloads to resolve the concrete return type from the actual
call args -- the single source of truth shared with the generated stubs."""

import typing

import pytest

from webclient.collection import Field
from webclient.core.document import Document, Element
from webclient.core.reference import Reference
from webclient.query.hints import (
    CALL_OP,
    attr_return_type,
    resolve_hints,
    return_type,
    safe_type_check,
)
from webclient.core.document.models import Signal, Transport


# -- safe_type_check (typeguard) --------------------------------------------
def test_safe_type_check_literal_and_scalars():
    assert safe_type_check("href", typing.Literal["href", "src", "action"])
    assert not safe_type_check("name", typing.Literal["href", "src", "action"])
    assert safe_type_check("x", str) and not safe_type_check(1, str)
    assert safe_type_check(anything := object(), typing.Any) and anything
    assert safe_type_check("x", "SomeForwardRef")  # unresolved string -> lenient


# -- overload narrowing: attr("href") vs attr("name") -----------------------
def test_attr_href_narrows_to_reference_core():
    assert return_type(Document, "attr", ("href",)) is Reference
    assert return_type(Document, "attr", ("src",)) is Reference
    assert return_type(Document, "attr", ("action",)) is Reference


def test_attr_other_is_field_str():
    got = return_type(Document, "attr", ("name",))
    assert typing.get_origin(got) is Field and typing.get_args(got) == (str,)


def test_attr_with_error_kwarg_still_binds_str_overload():
    from webclient.errors import RETURN

    got = return_type(Document, "attr", ("data-x",), {"error": RETURN})
    assert typing.get_origin(got) is Field


# -- render overloads: format-literal narrowing -----------------------------
def test_render_format_overloads():
    assert return_type(Document, "render", ("markdown",)) is str
    links = return_type(Document, "render", ("links",))
    assert typing.get_origin(links) is list
    assert typing.get_args(links) == (Reference,)
    elements = return_type(Document, "render", ("elements",))
    assert typing.get_args(elements) == (Element,)


# -- plain ops + props ------------------------------------------------------
def test_select_and_select_all():
    assert return_type(Document, "select", ("a",)) is Document
    got = return_type(Document, "select_all", ("a",))
    assert typing.get_origin(got) is list and typing.get_args(got) == (Document,)


def test_text_content_prop_is_str():
    # the backing declares str | None; the eager tier collapses the union to str
    # later (in the recorder's annotation mapping), but the raw resolution keeps it
    assert return_type(Document, "text_content") == (str | None)


def test_facet_ops_resolve_their_models():
    assert return_type(Document, "transport") is Transport
    assert return_type(Document, "spa") is Signal


def test_reference_resolve_returns_document_core():
    assert return_type(Reference, "resolve") is Document


# -- core data fields (not backing ops) -------------------------------------
def test_core_data_field_annotations():
    assert return_type(Document, "status_code") is int
    assert return_type(Document, "final_url") == (str | None)
    # a Literal-typed field passes through as its annotation
    assert typing.get_origin(return_type(Document, "kind")) is typing.Literal


def test_class_property_members_resolve():
    # `ok`/`url` are class @property members on the core (not backing ops, not
    # data fields) -- the resolver reads their fget return annotation.
    assert return_type(Document, "ok") is bool
    assert return_type(Reference, "ok") is bool
    assert return_type(Reference, "url") is str


def test_attr_return_type_splits_call_ops_from_value_attrs():
    # call ops -> CALL_OP (return known only once args are seen)
    assert attr_return_type(Document, "select") is CALL_OP
    assert attr_return_type(Document, "attr") is CALL_OP
    assert attr_return_type(Document, "render") is CALL_OP
    # prop ops / class @property / data fields -> the value type
    assert attr_return_type(Document, "text_content") == (str | None)
    assert attr_return_type(Document, "ok") is bool
    assert attr_return_type(Document, "status_code") is int
    with pytest.raises(AttributeError):
        attr_return_type(Document, "definitely_not_an_attr")


def test_unknown_op_raises():
    with pytest.raises(AttributeError):
        return_type(Document, "definitely_not_an_op")


# -- resolve_hints picks the FIRST matching overload (checker semantics) -----
def test_resolve_hints_first_match_order():
    from webclient.core.document.html import HtmlBacking

    # "href" satisfies BOTH the Literal overload and the str overload; the first
    # (Literal -> Reference) must win, as a static checker would choose.
    hints = resolve_hints(HtmlBacking.attr, ("href",), {})
    assert hints["return"] is Reference
