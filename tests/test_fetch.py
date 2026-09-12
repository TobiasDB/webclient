import pytest

from webclient import (
    RETURN,
    FetchError,
    NavigationEvent,
    NetworkEvent,
    Reference,
    WebClient,
)


@pytest.fixture
def wc():
    with WebClient() as client:
        yield client


def test_fetch_html_document(httpserver, wc):
    httpserver.expect_request("/page").respond_with_data(
        "<html><body><h1 class='t'>Hello</h1></body></html>",
        content_type="text/html; charset=utf-8")
    doc = wc.ref(httpserver.url_for("/page")).resolve()
    assert doc.ok and doc.kind == "html"
    assert doc.encoding == "utf-8"
    assert doc.select(".t").text == "Hello"
    assert doc.elapsed is not None
    assert doc.id and wc.document(doc.id) is doc


def test_fetch_sniffs_json(httpserver, wc):
    httpserver.expect_request("/api").respond_with_json({"n": 1})
    doc = wc.ref(httpserver.url_for("/api")).resolve()
    assert doc.kind == "json"
    import json as _j
    assert _j.loads(doc.text) == {"n": 1}


def test_fetch_records_navigation_event(httpserver, wc):
    httpserver.expect_request("/page").respond_with_data("ok", content_type="text/html")
    doc = wc.ref(httpserver.url_for("/page")).resolve()
    navs = doc.events_of(NavigationEvent)
    assert len(navs) == 1
    assert navs[0].document_id == doc.id
    assert navs[0].source == "core-network"
    assert navs[0].seq == 1


def test_fetch_records_redirect_hops(httpserver, wc):
    httpserver.expect_request("/start").respond_with_data(
        "", status=302, headers={"Location": httpserver.url_for("/end")})
    httpserver.expect_request("/end").respond_with_data("done", content_type="text/html")
    doc = wc.ref(httpserver.url_for("/start")).resolve()
    assert doc.final_url == httpserver.url_for("/end")
    hops = [e for e in doc.events_of(NetworkEvent)
            if not isinstance(e, NavigationEvent)]
    assert [h.status_code for h in hops] == [302]
    assert [n.status_code for n in doc.events_of(NavigationEvent)] == [200]


def test_fetch_non_2xx_raises_with_document(httpserver, wc):
    httpserver.expect_request("/gone").respond_with_data("nope", status=404)
    with pytest.raises(FetchError) as info:
        wc.ref(httpserver.url_for("/gone")).resolve()
    assert info.value.document is not None
    assert info.value.document.status_code == 404


def test_fetch_non_2xx_optional_returns_document(httpserver, wc):
    httpserver.expect_request("/gone").respond_with_data("nope", status=404)
    doc = wc.ref(httpserver.url_for("/gone")).resolve(error=RETURN)
    assert not doc.ok and doc.status_code == 404


def test_fetch_transport_error(wc):
    ref = Reference.from_url("http://127.0.0.1:1/nothing")  # port 1: refused
    with pytest.raises(FetchError):
        wc.fetch(ref)
    doc = wc.fetch(ref, optional=True)
    assert doc.status_code == 0 and not doc.ok


def test_fetch_sends_headers_params_and_method(httpserver, wc):
    httpserver.expect_request(
        "/submit", method="POST", query_string="q=1",
        headers={"x-app": "demo"},
    ).respond_with_json({"ok": True})
    ref = wc.ref(httpserver.url_for("/submit") + "?q=1", method="post")
    doc = ref.replace(headers={"x-app": "demo"}).resolve()
    import json as _j
    assert _j.loads(doc.text) == {"ok": True}


def test_reload_refetches(httpserver, wc):
    hits = {"n": 0}

    def handler(request):
        from werkzeug.wrappers import Response
        hits["n"] += 1
        return Response(f"hit {hits['n']}", content_type="text/html")

    httpserver.expect_request("/count").respond_with_handler(handler)
    doc = wc.ref(httpserver.url_for("/count")).resolve()
    again = doc.reload()
    assert doc.text == "hit 1" and again.text == "hit 2"
    assert again.id != doc.id


def test_fetched_document_renders(httpserver, wc):
    httpserver.expect_request("/a").respond_with_data(
        "<html><body><h1>Title</h1><p>Body</p></body></html>",
        content_type="text/html")
    doc = wc.ref(httpserver.url_for("/a")).resolve()
    assert "# Title" in doc.render("markdown")


def test_pool_reuses_clients(httpserver, wc):
    httpserver.expect_request("/x").respond_with_data("ok", content_type="text/html")
    for _ in range(3):
        wc.ref(httpserver.url_for("/x")).resolve()
    stats = wc.pool.stats()
    assert stats.http_total == 1 and stats.http_free == 1


def test_closed_client_refuses_work(httpserver):
    wc = WebClient()
    httpserver.expect_request("/y").respond_with_data("ok")
    wc.ref(httpserver.url_for("/y")).resolve()
    wc.close()
    with pytest.raises(RuntimeError, match="closed"):
        wc.ref(httpserver.url_for("/y")).resolve()


def test_default_client_recreated_after_close():
    from webclient import default_client
    first = default_client()
    first.close()
    second = default_client()
    assert second is not first and not second._closed
    second.close()


def test_engine_loop_reentrancy_guard(wc):
    loop = wc._ensure_loop()

    async def inner():
        return 1

    async def outer():
        return loop.run(inner())

    with pytest.raises(RuntimeError, match="loop thread"):
        loop.run(outer())
