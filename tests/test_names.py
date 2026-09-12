"""P1 gate: scoped names with retention (PLAN Decision 9) and doc.ref()."""
import pytest

from webclient import RETURN, Reference, WebClient


@pytest.fixture
def wc():
    with WebClient() as client:
        yield client


@pytest.fixture
def site(httpserver):
    for i in range(1, 7):
        httpserver.expect_request(f"/p{i}").respond_with_data(
            f"<h1>{i}</h1>", content_type="text/html")
    return httpserver


def test_resolve_names_ref_and_doc_in_the_client_scope(site, wc):
    ref = wc.ref(site.url_for("/p1"))
    doc = ref.resolve()
    assert ref.name == "ref:000-001" and doc.name == "doc:000-002"
    assert doc.root == ref.name and doc.id == doc.name
    assert wc.document(doc.name) is doc
    assert wc.reference(doc.root) is ref
    assert wc.document(doc.root) is None and wc.reference(doc.name) is None
    assert doc.ref() is ref
    assert doc.created and doc.accessed >= doc.created


def test_ref_roundtrips_request_and_action_chain(site, wc):
    doc = wc.ref(site.url_for("/p2")).resolve()
    wc._scope.clear()                                  # resolver forgot it
    rebuilt = doc.ref()
    assert rebuilt.name == doc.root and rebuilt.url == doc.url
    rebuilt.actions.append({"op": "click", "args": ["#go"]})
    again = Reference.model_validate_json(rebuilt.model_dump_json())
    assert again.model_dump() == rebuilt.model_dump()   # binding is private state
    assert wc.fetch(again).select("h1").text == "2"


def test_derived_references_are_unnamed_and_rooted(site, wc):
    doc = wc.ref(site.url_for("/p1")).resolve()
    derived = doc.ref().with_params(page="2")
    assert derived.name == "" and derived.root == doc.root


def test_session_scope_is_visible_to_client_but_not_to_other_sessions(site, wc):
    a, b = wc.session(), wc.session()
    doc = a.ref(site.url_for("/p3")).resolve()
    assert doc.name == "doc:001-002"                   # scope 001, after its ref
    assert a.document(doc.name) is doc
    assert wc.document(doc.name) is doc
    assert b.document(doc.name) is None
    a.close()                                          # retention ends
    assert wc.document(doc.name) is None and a.document(doc.name) is None


def test_client_scope_is_capped_lru(site):
    with WebClient(names_cap=4) as wc:
        docs = [wc.ref(site.url_for(f"/p{i}")).resolve() for i in range(1, 6)]
        assert len(wc._scope) == 4
        assert wc.document(docs[0].name) is None       # evicted
        assert wc.document(docs[-1].name) is docs[-1]


def test_failed_fetch_is_a_not_ok_document(site, wc):
    site.expect_request("/missing").respond_with_data("", status=404)
    doc = wc.ref(site.url_for("/missing")).resolve(error=RETURN)
    assert doc.ok is False and doc.error is not None
    assert doc.error.type == "HTTPStatus" and "404" in doc.message
    assert doc.is_ok().get() is False
