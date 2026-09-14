"""The typing gate: this file must pass mypy --strict and pyright.

It exercises the typed surface -- the lazy authoring roots (doc/ref/many/
reference), the eager materialised tier (Document/Reference/Field/Collection),
and the two-tier client surface (WebClient/AsyncWebClient with the lazy tier in
webclient.models). The stubs are generated/verified by scripts/gen_stubs.py.
"""

from typing import Any, assert_type

from webclient import (
    AsyncWebClient,
    Collection,
    Document,
    Element,
    Field,
    Reference,
    WebClient,
    doc,
    many,
    ref,
    reference,
)
from pydantic import BaseModel

from webclient.models import LazyDocument, LazyField, LazyReference


class _Row(BaseModel):
    title: str


# -- lazy authoring roots ---------------------------------------------------
assert_type(doc, Document)
assert_type(many, Collection[Document])
assert_type(ref, Reference)

# element ops on a Document
assert_type(doc.select("a").select_all("li"), Collection[Document])
assert_type(doc.text_content, str)  # eager scalar is raw; the lazy tier wraps it
assert_type(doc.attr("href"), Reference)  # link attrs narrow
assert_type(doc.attr("href").resolve().text_content, str)
assert_type(ref.resolve(), Document)

# a Field materialises to its value
title: Field[str] = Field[str]()
assert_type(title.get(), str)
assert_type(title.is_ok(), Field[bool])

# Collection lifts element ops and keeps the element type
assert_type(many.select("a"), Collection[Document])
# NOTE: the collection lift renders a scalar prop as a method (def text_content())
# whereas Document/LazyDocument expose it as a property -- a tier inconsistency to
# resolve in the vocabulary/type-safety pass (#4/#7).
assert_type(many.text_content(), Collection[Field[str]])
assert_type(many.filter(title == "x"), Collection[Document])
assert_type(many.extract(name=title), Collection[Document])
assert_type(many.project(), list[dict[str, Any]])
assert_type(many.project(_Row), list[_Row])  # schema-guided -> typed rows

# reference("url") is a lazy root, typed as Reference
assert_type(reference("https://e.com"), Reference)
assert_type(reference("https://e.com").resolve().select("a").text_content, str)

# extract -> project pipeline
assert_type(
    doc.select_all("li").extract(t=doc.text_content).project(), list[dict[str, Any]]
)

# render() is the single representation function, typed per format
res_doc = reference("https://e.com").resolve()
assert_type(res_doc.render("markdown"), str)
assert_type(res_doc.render("elements"), list[Element])
assert_type(res_doc.render("links"), Collection[Reference])

# iterating a Collection yields the element type
for _card in doc.select_all(".card"):
    assert_type(_card, Document)


# -- two-tier lazy client surface: entry points are lazy; collect()/execute()
#    materialise to the eager tier.
_wc = WebClient()
assert_type(_wc.ref("https://e.com"), LazyReference)
assert_type(_wc.lazy("https://e.com"), LazyReference)
assert_type(_wc.fetch("https://e.com"), LazyDocument)

# summary records a plan you collect (a Lazy[T] handle)
assert_type(_wc.summary("https://e.com").collect(), dict[str, Any])

# a lazy extract->project pipeline is itself a Lazy handle; collect() materialises
assert_type(
    _wc.fetch("https://e.com")
    .select_all(".card")
    .extract(t=doc.text_content)
    .project()
    .collect(),
    list[dict[str, Any]],
)
assert_type(_wc.fetch("https://e.com").select(".t"), LazyDocument)
assert_type(_wc.fetch("https://e.com").attr("href"), LazyReference)
assert_type(_wc.fetch("https://e.com").text_content, LazyField[str])
assert_type(_wc.fetch("https://e.com").collect(), Document)
assert_type(_wc.fetch("https://e.com").text_content.collect(), Field[str])
assert_type(_wc.ref("https://e.com").resolve().collect(), Document)
assert_type(
    _wc.ref("https://e.com").resolve().select(".t").text_content.collect(), Field[str]
)

# a client-bound plan collects on that client (the one realization path)
assert_type(_wc.ref("https://e.com").collect(), Reference)

# the async client awaits to the same materialised model
_ac = AsyncWebClient()
assert_type(_ac.fetch("https://e.com"), LazyDocument)


async def _async_surface() -> None:
    # acollect() is the one async realization (the twin of collect())
    assert_type(await _ac.fetch("https://e.com").acollect(), Document)
    assert_type(await _ac.ref("https://e.com").resolve().acollect(), Document)
    assert_type(await _ac.fetch("https://e.com").text_content.acollect(), Field[str])
