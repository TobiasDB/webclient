"""Runtime return-type resolution (webclient.query.hints): the recorder reads a
backing op's own overloads to resolve the concrete return type from the actual
call args -- the single source of truth shared with the generated stubs."""

import typing

import pytest

from webclient.collection import Field
from webclient.core.document import DocumentCore, Element
from webclient.core.reference import ReferenceCore
from webclient.query.hints import resolve_hints, return_type, safe_type_check
from webclient.summary import Summary, Transport


# -- safe_type_check (typeguard) --------------------------------------------
def test_safe_type_check_literal_and_scalars():
    assert safe_type_check("href", typing.Literal["href", "src", "action"])
    assert not safe_type_check("name", typing.Literal["href", "src", "action"])
    assert safe_type_check("x", str) and not safe_type_check(1, str)
    assert safe_type_check(anything := object(), typing.Any) and anything
    assert safe_type_check("x", "SomeForwardRef")  # unresolved string -> lenient


# -- overload narrowing: attr("href") vs attr("name") -----------------------
def test_attr_href_narrows_to_reference_core():
    assert return_type(DocumentCore, "attr", ("href",)) is ReferenceCore
    assert return_type(DocumentCore, "attr", ("src",)) is ReferenceCore
    assert return_type(DocumentCore, "attr", ("action",)) is ReferenceCore


def test_attr_other_is_field_str():
    got = return_type(DocumentCore, "attr", ("name",))
    assert typing.get_origin(got) is Field and typing.get_args(got) == (str,)


def test_attr_with_error_kwarg_still_binds_str_overload():
    from webclient.errors import RETURN

    got = return_type(DocumentCore, "attr", ("data-x",), {"error": RETURN})
    assert typing.get_origin(got) is Field


# -- render overloads: format-literal narrowing -----------------------------
def test_render_format_overloads():
    assert return_type(DocumentCore, "render", ("markdown",)) is str
    links = return_type(DocumentCore, "render", ("links",))
    assert typing.get_origin(links) is list
    assert typing.get_args(links) == (ReferenceCore,)
    elements = return_type(DocumentCore, "render", ("elements",))
    assert typing.get_args(elements) == (Element,)


# -- plain ops + props ------------------------------------------------------
def test_select_and_select_all():
    assert return_type(DocumentCore, "select", ("a",)) is DocumentCore
    got = return_type(DocumentCore, "select_all", ("a",))
    assert typing.get_origin(got) is list and typing.get_args(got) == (DocumentCore,)


def test_text_content_prop_is_str():
    # the backing declares str | None; the eager tier collapses the union to str
    # later (in the recorder's annotation mapping), but the raw resolution keeps it
    assert return_type(DocumentCore, "text_content") == (str | None)


def test_summary_and_transport_facets_resolve_models():
    assert return_type(DocumentCore, "summary") is Summary
    assert return_type(DocumentCore, "transport") is Transport


def test_reference_resolve_returns_document_core():
    assert return_type(ReferenceCore, "resolve") is DocumentCore


# -- core data fields (not backing ops) -------------------------------------
def test_core_data_field_annotations():
    assert return_type(DocumentCore, "status_code") is int
    assert return_type(DocumentCore, "final_url") == (str | None)
    # a Literal-typed field passes through as its annotation
    assert typing.get_origin(return_type(DocumentCore, "kind")) is typing.Literal


def test_class_property_members_resolve():
    # `ok`/`url` are class @property members on the core (not backing ops, not
    # data fields) -- the resolver reads their fget return annotation.
    assert return_type(DocumentCore, "ok") is bool
    assert return_type(ReferenceCore, "ok") is bool
    assert return_type(ReferenceCore, "url") is str


def test_unknown_op_raises():
    with pytest.raises(AttributeError):
        return_type(DocumentCore, "definitely_not_an_op")


# -- resolve_hints picks the FIRST matching overload (checker semantics) -----
def test_resolve_hints_first_match_order():
    from webclient.core.document.html import HtmlBacking

    # "href" satisfies BOTH the Literal overload and the str overload; the first
    # (Literal -> ReferenceCore) must win, as a static checker would choose.
    hints = resolve_hints(HtmlBacking.attr, ("href",), {})
    assert hints["return"] is ReferenceCore
