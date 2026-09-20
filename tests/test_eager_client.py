"""The core IS the eager client (option B): a ``WebClient`` is directly a
context-managed client whose ``fetch``/``ref`` dispatch to eager surfaces (a
``Document``/``Reference`` is its core), with no ``.collect()``. The lazy path is
opt-in via ``.lazy`` (built on top of this)."""

import pytest

from webclient.core.client import WebClient
from webclient.core.document import Document
from webclient.core.reference import Reference

PAGE = """
<html><body><h1>Hi</h1>
  <div class="card"><span class="t">Aeropress</span><a href="/i/1">go</a></div>
  <div class="card"><span class="t">Grinder</span><a href="/i/2">go</a></div>
</body></html>
"""


@pytest.fixture
def site(httpserver):
    httpserver.expect_request("/").respond_with_data(PAGE, content_type="text/html")
    return httpserver


def test_core_is_a_context_managed_eager_client(site):
    with WebClient() as c:
        doc = c.fetch(site.url_for("/"))  # eager -> a Document (its core)
        assert isinstance(doc, Document) and doc.ok
        assert doc.select("h1").attr("text") == "Hi"  # dispatch on the resolved core
        assert isinstance(doc.select("a").attr("href"), Reference)  # href narrows
    assert c._closed  # __exit__ closed the client


def test_eager_select_all_fans_out_to_a_collection(site):
    with WebClient() as c:
        doc = c.fetch(site.url_for("/"))
        titles = doc.select_all(".card").select(".t").attr("text")
        assert titles == ["Aeropress", "Grinder"]


def test_eager_ref_is_a_reference_core(site):
    with WebClient() as c:
        ref = c.ref(site.url_for("/"))
        assert isinstance(ref, Reference)
        assert ref.resolve().ok  # resolve dispatches eagerly to a Document


def test_document_extract_is_the_single_element_form(site):
    # extract evaluates >1 expression against ONE document (a "collection of one");
    # it returns the document (so extracts chain) and project renders a single row.
    from webclient import wq

    with WebClient() as c:
        doc = c.fetch(site.url_for("/"))
        staged = doc.extract(
            heading=wq.doc.select("h1").attr("text"),
            first=wq.doc.select(".t").attr("text"),
        )
        assert staged is doc  # extract returns the document itself
        row = staged.project()
        assert row == {"heading": "Hi", "first": "Aeropress"}  # one dict, not a list
        # chained extracts accumulate onto the same row
        assert doc.extract(link=wq.doc.select("a").attr("href")).project()["link"].endswith(
            "/i/1"
        )  # a Reference column projects to its URL string




def test_fetch_hops_reuse_one_doc_name(monkeypatch):
    # M3: the transport hops of ONE fetch (static -> proxy -> browser) must share a single
    # scope slot, not register 2-3 orphaned docs. _register(reuse=<name>) rebinds the slot.
    with WebClient() as wc:
        ref = wc.ref("https://x.co/")

        def n_docs():
            return sum(1 for k in wc._scope._items if k.startswith("doc:"))

        d1 = Document(url="https://x.co/", status_code=200, content=b"<p>1</p>", kind="html")
        wc._register(d1, ref)
        name = d1.name
        assert n_docs() == 1

        d2 = Document(url="https://x.co/", status_code=200, content=b"<p>2</p>", kind="html")
        wc._register(d2, ref, reuse=name)  # a later hop of the SAME fetch
        assert d2.name == name and n_docs() == 1  # still ONE doc slot, not two
        assert wc.document(name) is d2  # the slot now holds the latest hop

        # a genuinely separate fetch still gets its own slot
        d3 = Document(url="https://y.co/", status_code=200, content=b"<p>3</p>", kind="html")
        wc._register(d3, wc.ref("https://y.co/"))
        assert d3.name != name and n_docs() == 2
