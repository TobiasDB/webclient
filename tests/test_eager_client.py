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
        assert doc.select("h1").text_content == "Hi"  # dispatch on the resolved core
        assert isinstance(doc.select("a").attr("href"), Reference)  # href narrows
    assert c._closed  # __exit__ closed the client


def test_eager_select_all_fans_out_to_a_collection(site):
    with WebClient() as c:
        doc = c.fetch(site.url_for("/"))
        titles = doc.select_all(".card").select(".t").text_content()
        assert titles == ["Aeropress", "Grinder"]


def test_eager_ref_is_a_reference_core(site):
    with WebClient() as c:
        ref = c.ref(site.url_for("/"))
        assert isinstance(ref, Reference)
        assert ref.resolve().ok  # resolve dispatches eagerly to a Document
