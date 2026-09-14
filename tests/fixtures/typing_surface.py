"""The typing gate: this file must pass mypy --strict and pyright.

It exercises the typed surface in three parts:
  * the lazy authoring namespace ``wq`` (``wq.doc``/``ref``/``many`` +
    ``wq.reference``) -- every op returns a lazy type, and ``collect``/``stream``/
    ``_plan``/``field``/``reference`` are visible to the checker;
  * the eager materialised tier (Document/Reference/Field/Collection), reached
    from genuinely eager values (``client.fetch(...).collect()``);
  * the two-tier client surface (WebClient/AsyncWebClient, lazy tier in
    webclient.models).
The stubs are generated/verified by scripts/gen_stubs.py.
"""

from typing import Any, assert_type

from pydantic import BaseModel

from webclient import (
    AsyncWebClient,
    Collection,
    Document,
    Element,
    Field,
    Reference,
    WebClient,
    wq,
)
from webclient.models import (
    Lazy,
    LazyCollection,
    LazyDocument,
    LazyField,
    LazyReference,
)
from webclient.query.plan import Plan


class _Row(BaseModel):
    title: str


# -- lazy authoring namespace (wq): roots are lazy, ops return lazy types ----
assert_type(wq.doc, LazyDocument)
assert_type(wq.ref, LazyReference)
assert_type(wq.many, LazyCollection[LazyDocument])

assert_type(wq.doc.select("a").select_all("li"), LazyCollection[LazyDocument])
assert_type(wq.doc.text_content, LazyField[str])
assert_type(wq.doc.attr("href"), LazyReference)  # link attrs narrow
assert_type(wq.doc.attr("name"), LazyField[str])
assert_type(wq.doc.attr("href").resolve().text_content, LazyField[str])
assert_type(wq.doc.field("x"), LazyField[Any])  # recorder-only helpers
assert_type(wq.doc.reference("x"), LazyReference)
assert_type(wq.ref.resolve(), LazyDocument)

# wq.reference(url) roots a lazy plan at a URL
assert_type(wq.reference("https://e.com"), LazyReference)
assert_type(
    wq.reference("https://e.com").resolve().select("a").text_content, LazyField[str]
)

# the lazy collection lift keeps element ops (fan-out), then the row-shaping ops
assert_type(wq.many.select("a"), LazyCollection[LazyDocument])
assert_type(wq.many.text_content, LazyCollection[LazyField[str]])
assert_type(wq.doc.select_all(".t").attr("name"), LazyCollection[LazyField[str]])
assert_type(wq.many.filter(wq.doc.field("x")), LazyCollection[LazyDocument])
assert_type(wq.many.extract(name=wq.doc.text_content), LazyCollection[LazyDocument])

# extract -> project -> a Lazy[list[dict]] handle you collect (or introspect)
_rows = wq.ref.resolve().select_all(".card").extract(t=wq.doc.text_content).project()
assert_type(_rows, Lazy[list[dict[str, Any]]])
assert_type(_rows.collect(), list[dict[str, Any]])
assert_type(_rows._plan, Plan)  # introspection is typed
assert_type(wq.doc.select(".t").text_content.collect(), Field[str])
assert_type(wq.doc.select(".t").text_content._plan, Plan)


# -- eager materialised tier: from genuinely eager values --------------------
_wc = WebClient()
_page = _wc.fetch("https://e.com").collect()
assert_type(_page, Document)
assert_type(_page.select("a").select_all("li"), Collection[Document])
assert_type(_page.text_content, str)  # an eager scalar is raw
assert_type(_page.attr("href"), Reference)
assert_type(_page.render("markdown"), str)
assert_type(_page.render("elements"), list[Element])
assert_type(_page.render("links"), Collection[Reference])

_cards = _page.select_all(".card")
assert_type(_cards, Collection[Document])
assert_type(_cards.select("a"), Collection[Document])
assert_type(_cards.project(), list[dict[str, Any]])
assert_type(_cards.project(_Row), list[_Row])  # schema-guided -> typed rows

_title: Field[str] = Field[str]()
assert_type(_title.get(), str)
assert_type(_title.is_ok(), Field[bool])

for _card in _page.select_all(".card"):  # iterating a Collection yields the element
    assert_type(_card, Document)


# -- two-tier client surface: entry points lazy; collect() -> eager ----------
assert_type(_wc.ref("https://e.com"), LazyReference)
assert_type(_wc.lazy("https://e.com"), LazyReference)
assert_type(_wc.fetch("https://e.com"), LazyDocument)
assert_type(_wc.summary("https://e.com").collect(), dict[str, Any])
assert_type(
    _wc.fetch("https://e.com")
    .select_all(".card")
    .extract(t=wq.doc.text_content)
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
assert_type(_wc.ref("https://e.com").collect(), Reference)

_ac = AsyncWebClient()
assert_type(_ac.fetch("https://e.com"), LazyDocument)


async def _async_surface() -> None:
    # acollect() is the one async realization (the twin of collect())
    assert_type(await _ac.fetch("https://e.com").acollect(), Document)
    assert_type(await _ac.ref("https://e.com").resolve().acollect(), Document)
    assert_type(await _ac.fetch("https://e.com").text_content.acollect(), Field[str])
