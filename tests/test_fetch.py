import pytest

from webclient import (
    RETURN,
    FetchError,
    NavigationEvent,
    NetworkEvent,
    Reference,
    WebClient,
    from_url,
)


@pytest.fixture
def wc():
    with WebClient() as client:
        yield client


def test_fetch_html_document(httpserver, wc):
    httpserver.expect_request("/page").respond_with_data(
        "<html><body><h1 class='t'>Hello</h1></body></html>",
        content_type="text/html; charset=utf-8",
    )
    doc = wc.ref(httpserver.url_for("/page")).resolve().collect()
    assert doc.ok and doc.kind == "html"
    assert doc.encoding == "utf-8"
    assert doc.select(".t").text_content == "Hello"
    assert doc.elapsed is not None
    assert doc.id and wc.document(doc.id) is doc


def test_fetch_sniffs_json(httpserver, wc):
    httpserver.expect_request("/api").respond_with_json({"n": 1})
    doc = wc.ref(httpserver.url_for("/api")).resolve().collect()
    assert doc.kind == "json"
    import json as _j

    assert _j.loads(doc.text_content) == {"n": 1}


def test_fetch_records_navigation_event(httpserver, wc):
    httpserver.expect_request("/page").respond_with_data("ok", content_type="text/html")
    doc = wc.ref(httpserver.url_for("/page")).resolve().collect()
    navs = doc.events_of(NavigationEvent)
    assert len(navs) == 1
    assert navs[0].document_id == doc.id
    assert navs[0].source == "core-network"
    assert navs[0].seq == 1


def test_fetch_records_redirect_hops(httpserver, wc):
    httpserver.expect_request("/start").respond_with_data(
        "", status=302, headers={"Location": httpserver.url_for("/end")}
    )
    httpserver.expect_request("/end").respond_with_data(
        "done", content_type="text/html"
    )
    doc = wc.ref(httpserver.url_for("/start")).resolve().collect()
    assert doc.final_url == httpserver.url_for("/end")
    hops = [
        e for e in doc.events_of(NetworkEvent) if not isinstance(e, NavigationEvent)
    ]
    assert [h.status_code for h in hops] == [302]
    assert [n.status_code for n in doc.events_of(NavigationEvent)] == [200]


def test_fetch_non_2xx_raises_with_document(httpserver, wc):
    httpserver.expect_request("/gone").respond_with_data("nope", status=404)
    with pytest.raises(FetchError) as info:
        wc.ref(httpserver.url_for("/gone")).resolve().collect()
    assert info.value.document is not None
    assert info.value.document.status_code == 404


def test_fetch_non_2xx_optional_returns_document(httpserver, wc):
    httpserver.expect_request("/gone").respond_with_data("nope", status=404)
    doc = wc.ref(httpserver.url_for("/gone")).resolve(error=RETURN).collect()
    assert not doc.ok and doc.status_code == 404
    assert doc.error.retriable is False  # a 404 will not succeed on retry


def test_fetch_transport_error(wc):
    ref = from_url("http://127.0.0.1:1/nothing")  # port 1: refused
    with pytest.raises(FetchError):
        wc.fetch(ref).collect()
    doc = wc.fetch(ref, optional=True).collect()
    assert doc.status_code == 0 and not doc.ok
    assert doc.error.retriable is True  # a transport failure is worth a retry


def test_fetch_5xx_is_retriable(httpserver, wc):
    httpserver.expect_request("/boom").respond_with_data("no", status=503)
    doc = wc.ref(httpserver.url_for("/boom")).resolve(error=RETURN).collect()
    assert doc.status_code == 503 and doc.error.retriable is True


def test_ssrf_guard_blocks_loopback_when_enabled():
    """With the opt-in SSRF guard, a loopback/private host is refused before any
    transport happens."""
    with WebClient(block_private_hosts=True) as bwc:
        with pytest.raises(FetchError, match="blocked"):
            bwc.fetch("http://127.0.0.1:9/x").collect()
        doc = bwc.fetch("http://127.0.0.1:9/x", optional=True).collect()
        assert not doc.ok and doc.error.type == "BlockedHost"


def test_ssrf_guard_blocks_browser_navigation_before_launch():
    """The guard runs in afetch before the browser branch, so browser=True to a
    blocked host is refused without launching a page (needs no playwright)."""
    with WebClient(block_private_hosts=True) as bwc:
        doc = (
            bwc.ref("http://127.0.0.1:9/x")
            .resolve(browser=True, optional=True)
            .collect()
        )
        assert not doc.ok and doc.error.type == "BlockedHost"


def test_ssrf_guard_off_by_default_allows_loopback(httpserver, wc):
    httpserver.expect_request("/ok").respond_with_data("hi")  # served on 127.0.0.1
    doc = wc.ref(httpserver.url_for("/ok")).resolve().collect()
    assert doc.ok  # the default policy does not block loopback


def test_retries_recover_from_a_retriable_failure(httpserver):
    from werkzeug.wrappers import Response

    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return Response("busy", status=503)
        return Response("<html><title>ok</title></html>", content_type="text/html")

    httpserver.expect_request("/flaky").respond_with_handler(flaky)
    with WebClient(retries=3, retry_backoff=0.0) as wc:
        doc = wc.fetch(httpserver.url_for("/flaky")).collect()
    assert doc.ok and calls["n"] == 3


def test_no_retries_by_default(httpserver):
    from werkzeug.wrappers import Response

    calls = {"n": 0}

    def always_503(request):
        calls["n"] += 1
        return Response("busy", status=503)

    httpserver.expect_request("/down").respond_with_handler(always_503)
    with WebClient() as wc:
        doc = wc.fetch(httpserver.url_for("/down"), optional=True).collect()
    assert not doc.ok and doc.status_code == 503 and calls["n"] == 1


def test_fetch_sends_headers_params_and_method(httpserver, wc):
    httpserver.expect_request(
        "/submit",
        method="POST",
        query_string="q=1",
        headers={"x-app": "demo"},
    ).respond_with_json({"ok": True})
    ref = wc.ref(httpserver.url_for("/submit") + "?q=1", method="post")
    doc = ref.replace(headers={"x-app": "demo"}).resolve().collect()
    import json as _j

    assert _j.loads(doc.text_content) == {"ok": True}


def test_reload_refetches(httpserver, wc):
    hits = {"n": 0}

    def handler(request):
        from werkzeug.wrappers import Response

        hits["n"] += 1
        return Response(f"hit {hits['n']}", content_type="text/html")

    httpserver.expect_request("/count").respond_with_handler(handler)
    doc = wc.ref(httpserver.url_for("/count")).resolve().collect()
    again = doc.reload()
    assert doc.text_content == "hit 1" and again.text_content == "hit 2"
    assert again.id != doc.id


def test_fetched_document_renders(httpserver, wc):
    httpserver.expect_request("/a").respond_with_data(
        "<html><body><h1>Title</h1><p>Body</p></body></html>", content_type="text/html"
    )
    doc = wc.ref(httpserver.url_for("/a")).resolve().collect()
    assert "# Title" in doc.render("markdown")


def test_pool_reuses_clients(httpserver, wc):
    httpserver.expect_request("/x").respond_with_data("ok", content_type="text/html")
    for _ in range(3):
        wc.ref(httpserver.url_for("/x")).resolve().collect()
    stats = wc.pool.stats()
    assert stats.http_total == 1 and stats.http_free == 1


def test_closed_client_refuses_work(httpserver):
    wc = WebClient()
    httpserver.expect_request("/y").respond_with_data("ok")
    wc.ref(httpserver.url_for("/y")).resolve().collect()
    wc.close()
    with pytest.raises(RuntimeError, match="closed"):
        wc.ref(httpserver.url_for("/y")).resolve().collect()


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


def test_retry_after_parsing():
    import time
    from email.utils import formatdate

    from webclient.core.client import _retry_after_seconds

    assert _retry_after_seconds("5") == 5.0
    assert _retry_after_seconds("  3 ") == 3.0
    assert _retry_after_seconds(None) is None
    assert _retry_after_seconds("soon") is None
    future = _retry_after_seconds(formatdate(time.time() + 10, usegmt=True))
    assert future is not None and 5 < future <= 10


def test_retry_honours_retry_after_over_backoff(httpserver):
    import time

    from werkzeug.wrappers import Response

    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return Response("busy", status=503, headers={"Retry-After": "0"})
        return Response("<html><title>ok</title></html>", content_type="text/html")

    httpserver.expect_request("/ra").respond_with_handler(flaky)
    # a large backoff: if Retry-After were ignored the retry would wait ~5s
    with WebClient(retries=1, retry_backoff=5.0) as wc:
        start = time.monotonic()
        doc = wc.fetch(httpserver.url_for("/ra")).collect()
        elapsed = time.monotonic() - start
    assert doc.ok and calls["n"] == 2 and elapsed < 2.0


def test_min_interval_paces_same_host_requests(httpserver):
    import time

    httpserver.expect_request("/p").respond_with_data("ok", content_type="text/html")
    url = httpserver.url_for("/p")
    with WebClient(min_interval=0.3) as wc:
        wc.fetch(url).collect()  # first request: sets the next-allowed time
        start = time.monotonic()
        wc.fetch(url).collect()  # second: paced ~0.3s after the first started
        elapsed = time.monotonic() - start
    assert elapsed >= 0.2  # generous margin; without pacing this is ~0
