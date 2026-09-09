"""R0: resolving references into documents, and the capability model."""
import pytest

from webclient import (Document, Reference, ResolveError, StaleDocument,
                       UnsupportedOperation, WebClient)


def test_resolve_returns_a_document(site, wc):
    document = wc.resolve(site.url_for("/cards"))
    assert document.status_code.get() == 200
    assert document.ok.get() is True
    assert document.attr("title").get() == "Laptops"
    assert document.select("h1").attr("text").get() == "Laptops"


def test_relative_links_resolve_against_the_url_that_answered(site, wc):
    """The redirect bug the old Document(Reference) inheritance caused."""
    document = wc.resolve(site.url_for("/redir"))
    assert document.final_url.get().endswith("/deep/page")
    link = document.select("a").attr("href")   # link attrs narrow
    assert link.url.endswith("/deep/c.html"), link.url


def test_redirect_hops_are_recorded(site, wc):
    document = wc.resolve(site.url_for("/redir"))
    assert [r.status for r in document.telemetry.redirects] == [302]
    assert len(document.telemetry.requests) == 2


def test_non_2xx_raises_with_the_document_attached(site, wc):
    with pytest.raises(ResolveError) as caught:
        wc.resolve(site.url_for("/missing"))
    assert caught.value.status_code == 404
    assert caught.value.document.status_code.get() == 404


def test_optional_returns_the_not_ok_document(site, wc):
    document = wc.resolve(site.url_for("/missing"), optional=True)
    assert document.ok.get() is False
    assert document.message.get() == "HTTP 404"


def test_transport_failure_still_yields_a_document(wc):
    document = wc.resolve("http://127.0.0.1:9/nothing", optional=True)
    assert document.status_code.get() == 0
    assert document.message.get()


def test_browser_op_on_an_http_backing_is_a_typed_error(site, wc):
    document = wc.resolve(site.url_for("/cards"))
    assert document.supports("browser") is False
    with pytest.raises(UnsupportedOperation) as caught:
        document.click(".next")
    assert "browser" in str(caught.value)
    assert "browser=True" in str(caught.value)


def test_documents_are_addressable_by_id(site, wc):
    document = wc.resolve(site.url_for("/cards"))
    assert wc.document(document.id) is document


def test_from_content_needs_no_client():
    document = Document.from_content("<p>hi</p>", url="https://x.test/a/b")
    assert document.select("p").attr("text").get() == "hi"
    assert document.render("markdown").get() == "hi"
