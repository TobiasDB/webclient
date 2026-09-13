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
    SearchEngine,
    WebClient,
    doc,
    many,
    ref,
    reference,
)
from webclient.models import LazyDocument, LazyField, LazyReference

# -- lazy authoring roots ---------------------------------------------------
assert_type(doc, Document)
assert_type(many, Collection[Document])
assert_type(ref, Reference)

# element ops on a Document
assert_type(doc.select("a").select_all("li"), Collection[Document])
assert_type(doc.attr("text"), Field[str])
assert_type(doc.attr("href"), Reference)  # link attrs narrow
assert_type(doc.attr("href").resolve().attr("text"), Field[str])
assert_type(ref.resolve(), Document)

# a Field materialises to its value
title: Field[str] = Field[str]()
assert_type(title.get(), str)
assert_type(title.is_ok(), Field[bool])

# Collection lifts element ops and keeps the element type
assert_type(many.select("a"), Collection[Document])
assert_type(many.attr("text"), Collection[Field[str]])
assert_type(many.filter(title == "x"), Collection[Document])
assert_type(many.extract(name=title), Collection[Document])
assert_type(many.project(), list[dict[str, Any]])

# reference("url") is a lazy root, typed as Reference
assert_type(reference("https://e.com"), Reference)
assert_type(reference("https://e.com").resolve().select("a").attr("text"), Field[str])

# extract -> project pipeline
assert_type(
    doc.select_all("li").extract(t=doc.attr("text")).project(), list[dict[str, Any]]
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

# higher-level authoring verbs record a plan you collect (a Lazy[T] handle)
_engine = SearchEngine(url="https://e.com/s?q={q}")
assert_type(_wc.search("coffee", engine=_engine).collect(), list[dict[str, Any]])
assert_type(_wc.summary("https://e.com").collect(), dict[str, Any])
assert_type(_wc.fetch("https://e.com").select(".t"), LazyDocument)
assert_type(_wc.fetch("https://e.com").attr("href"), LazyReference)
assert_type(_wc.fetch("https://e.com").attr("text"), LazyField[str])
assert_type(_wc.fetch("https://e.com").collect(), Document)
assert_type(_wc.fetch("https://e.com").attr("text").collect(), Field[str])
assert_type(_wc.ref("https://e.com").resolve().collect(), Document)
assert_type(
    _wc.ref("https://e.com").resolve().select(".t").attr("text").collect(), Field[str]
)

# execute() materialises a lazy tier to its model via the Lazy[T] bridge
assert_type(_wc.execute(_wc.fetch("https://e.com")), Document)
assert_type(_wc.execute(_wc.ref("https://e.com")), Reference)
assert_type(_wc.execute(_wc.fetch("https://e.com").attr("text")), Field[str])

# the async client awaits to the same materialised model
_ac = AsyncWebClient()
assert_type(_ac.fetch("https://e.com"), LazyDocument)


async def _async_surface() -> None:
    assert_type(await _ac.execute(_ac.fetch("https://e.com")), Document)
    assert_type(await _ac.execute(_ac.ref("https://e.com").resolve()), Document)
    assert_type(await _ac.execute(_ac.fetch("https://e.com").attr("text")), Field[str])
