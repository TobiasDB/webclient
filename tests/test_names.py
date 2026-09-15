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
            f"<h1>{i}</h1>", content_type="text/html"
        )
    return httpserver


def test_resolve_names_ref_and_doc_in_the_client_scope(site, wc):
    # Names are assigned when the plan runs (collect), to the real Reference/
    # Document the engine builds -- not to the lazy wc.ref() expression.
    doc = wc.ref(site.url_for("/p1")).resolve().collect()
    assert doc.name == "doc:000-002" and doc.root == "ref:000-001"
    assert doc.id == doc.name
    assert wc.document(doc.name) is doc
    ref = wc.reference(doc.root)  # the registered reference
    assert ref is not None and ref.name == doc.root
    assert wc.document(doc.root) is None and wc.reference(doc.name) is None
    assert doc.ref() is ref  # doc.ref() is that reference
    assert doc.created and doc.accessed >= doc.created


def test_ref_roundtrips_request_and_action_chain(site, wc):
    doc = wc.ref(site.url_for("/p2")).resolve().collect()
    wc._scope.clear()  # resolver forgot it
    rebuilt = doc.ref()
    assert rebuilt.name == doc.root and rebuilt.url == doc.url
    rebuilt.actions.append({"op": "click", "args": ["#go"]})
    again = Reference.model_validate_json(rebuilt.model_dump_json())
    assert again.model_dump() == rebuilt.model_dump()  # binding is private state
    assert wc.fetch(again).collect().select("h1").text_content == "2"


def test_derived_references_are_unnamed_and_rooted(site, wc):
    doc = wc.ref(site.url_for("/p1")).resolve().collect()
    derived = doc.ref().with_params(page="2")
    assert derived.name == "" and derived.root == doc.root


def test_session_scope_is_visible_to_client_but_not_to_other_sessions(site, wc):
    a, b = wc.session(), wc.session()
    doc = a.ref(site.url_for("/p3")).resolve().collect()
    assert doc.name == "doc:001-002"  # scope 001, after its ref
    assert a.document(doc.name) is doc
    assert wc.document(doc.name) is doc
    assert b.document(doc.name) is None
    a.close()  # retention ends
    assert wc.document(doc.name) is None and a.document(doc.name) is None


def test_client_scope_is_capped_lru(site):
    with WebClient(names_cap=4) as wc:
        docs = [wc.ref(site.url_for(f"/p{i}")).resolve().collect() for i in range(1, 6)]
        assert len(wc._scope) == 4
        assert wc.document(docs[0].name) is None  # evicted
        assert wc.document(docs[-1].name) is docs[-1]


def test_failed_fetch_is_a_not_ok_document(site, wc):
    site.expect_request("/missing").respond_with_data("", status=404)
    doc = wc.ref(site.url_for("/missing")).resolve(error=RETURN).collect()
    assert doc.ok is False and doc.error is not None
    assert doc.error.type == "HTTPStatus" and "404" in doc.message
    assert doc.is_ok().get() is False


def test_name_scope_is_thread_safe_under_concurrent_add_get():
    """NameScope.add runs on the engine loop while .get runs on the caller's
    thread; the lock keeps concurrent OrderedDict mutation from corrupting it."""
    import threading

    from webclient.core.client import NameScope

    scope = NameScope(0, cap=64)
    errors: list[BaseException] = []
    names: list[str] = []
    lock = threading.Lock()

    def adder() -> None:
        try:
            for _ in range(500):
                n = scope.add("doc", object())
                with lock:
                    names.append(n)
        except BaseException as exc:  # a race would raise here
            errors.append(exc)

    def getter() -> None:
        try:
            for _ in range(500):
                with lock:
                    sample = names[-1] if names else None
                if sample is not None:
                    scope.get(sample)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=adder) for _ in range(4)]
    threads += [threading.Thread(target=getter) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors  # no mutated-during-iteration / KeyError
    assert len(scope) == 64  # cap held exactly, no lost/duplicated entries


def test_new_scope_indices_are_unique_under_concurrency():
    """Two sessions created off-thread must not collide on a scope index."""
    import threading

    with WebClient() as wc:
        indices: list[int] = []
        lock = threading.Lock()

        def make() -> None:
            for _ in range(50):
                s = wc.core.new_scope()
                with lock:
                    indices.append(s.index)

        threads = [threading.Thread(target=make) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(indices) == len(set(indices))  # all unique, none lost to a race
