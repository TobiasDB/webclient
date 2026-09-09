"""R5: sessions, middleware and pagination."""
import pytest

from webclient import Session, WebClient, WebClientError, doc, el


def test_cookies_persist_across_resolves(httpserver, wc):
    httpserver.expect_request("/set").respond_with_data(
        "ok", headers={"Set-Cookie": "token=abc; Path=/"})
    httpserver.expect_request("/echo").respond_with_handler(
        lambda r: __import__("werkzeug").wrappers.Response(
            r.headers.get("Cookie", "")))
    session = wc.session()
    session.resolve(httpserver.url_for("/set"))
    assert session.cookies["token"] == "abc"
    echoed = session.resolve(httpserver.url_for("/echo"))
    assert "token=abc" in echoed.attr("body").get()


def test_session_headers_are_merged(httpserver, wc):
    httpserver.expect_request("/h").respond_with_handler(
        lambda r: __import__("werkzeug").wrappers.Response(
            r.headers.get("X-Tenant", "")))
    session = wc.session(headers={"X-Tenant": "acme"})
    assert session.resolve(httpserver.url_for("/h")).attr("body").get() == "acme"


def test_expired_sessions_refuse_work(wc):
    session = wc.session(ttl=-1)
    assert session.alive is False
    with pytest.raises(WebClientError, match="expired"):
        session.resolve("https://example.com")


def test_retry_middleware_records_attempts(httpserver):
    state = {"n": 0}

    def flaky(request):
        import werkzeug
        state["n"] += 1
        status = 503 if state["n"] == 1 else 200
        return werkzeug.wrappers.Response("body", status=status)

    httpserver.expect_request("/flaky").respond_with_handler(flaky)
    from webclient.middleware import default_stack, observe, redirects, retry
    with WebClient(middleware=[redirects(), retry(2, on_status=(503,)),
                               observe()]) as client:
        document = client.resolve(httpserver.url_for("/flaky"))
        assert document.status_code.get() == 200
        assert [r.reason for r in document.telemetry.retries] == ["status 503"]


PAGE = """<html><body><span class="n">{n}</span>{link}</body></html>"""


@pytest.fixture
def paged(httpserver):
    for n in (1, 2, 3):
        link = (f'<a class="next" href="/p/{n + 1}">next</a>' if n < 3 else "")
        httpserver.expect_request(f"/p/{n}").respond_with_data(
            PAGE.format(n=n, link=link), content_type="text/html")
    return httpserver


def test_paginate_follows_a_selector(paged, wc):
    start = wc.resolve(paged.url_for("/p/1"))
    seen = [d.select(".n").attr("text").get()
            for d in wc.paginate(start, ".next")]
    assert seen == ["1", "2", "3"]


def test_paginate_respects_limit(paged, wc):
    start = wc.resolve(paged.url_for("/p/1"))
    assert len(list(wc.paginate(start, ".next", limit=2))) == 2


def test_paginate_stops_on_until(paged, wc):
    start = wc.resolve(paged.url_for("/p/1"))
    pages = list(wc.paginate(start, ".next",
                             until=lambda d: d.select(".n").attr("text") == "3"))
    assert len(pages) == 2


def test_paginate_over_an_iterable_of_params(paged, wc):
    start = wc.resolve(paged.url_for("/p/1"))
    pages = list(wc.paginate(start, [paged.url_for("/p/2"),
                                     paged.url_for("/p/3")]))
    assert [d.select(".n").attr("text").get() for d in pages] == ["1", "2", "3"]


def test_scrape_is_composition(paged, wc):
    out = wc.scrape(paged.url_for("/p/1"), formats=("markdown", "elements"))
    assert out["status"] == 200
    assert "1" in out["formats"]["markdown"]
    assert out["formats"]["elements"]


def test_pool_stats_report_leases(paged, wc):
    wc.resolve(paged.url_for("/p/1"))
    stats = wc.stats()
    assert stats.http_held == 0 and stats.http_total >= 1
