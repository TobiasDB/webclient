import time

import pytest
from werkzeug.wrappers import Response

from webclient import WebClient


@pytest.fixture
def wc():
    with WebClient() as client:
        yield client


def test_session_cookies_persist_across_fetches(httpserver, wc):
    httpserver.expect_request("/login").respond_with_response(
        Response(
            "ok",
            content_type="text/html",
            headers={"Set-Cookie": "token=abc123; Path=/"},
        )
    )

    def whoami(request):
        return Response(
            f"cookie={request.cookies.get('token')}", content_type="text/html"
        )

    httpserver.expect_request("/whoami").respond_with_handler(whoami)

    session = wc.session()
    session.ref(httpserver.url_for("/login")).resolve().collect()
    assert session.cookies == {"token": "abc123"}
    doc = session.ref(httpserver.url_for("/whoami")).resolve().collect()
    assert doc.text_content == "cookie=abc123"


def test_session_absorbs_cookie_with_dated_expires(httpserver, wc):
    """A cookie whose Expires attribute contains a comma must not be corrupted
    (the old hand-split on ", " broke it)."""
    httpserver.expect_request("/login").respond_with_response(
        Response(
            "ok",
            content_type="text/html",
            headers={
                "Set-Cookie": "sid=xyz; Expires=Wed, 21 Oct 2099 07:28:00 GMT; Path=/"
            },
        )
    )
    session = wc.session()
    session.ref(httpserver.url_for("/login")).resolve().collect()
    assert session.cookies == {"sid": "xyz"}


def test_sessions_are_isolated(httpserver, wc):
    httpserver.expect_request("/login").respond_with_response(
        Response("ok", headers={"Set-Cookie": "token=s1; Path=/"})
    )

    def whoami(request):
        return Response(f"cookie={request.cookies.get('token')}")

    httpserver.expect_request("/whoami").respond_with_handler(whoami)

    s1, s2 = wc.session(), wc.session()
    s1.ref(httpserver.url_for("/login")).resolve().collect()
    doc = s2.ref(httpserver.url_for("/whoami")).resolve().collect()
    assert doc.text_content == "cookie=None"  # s1's cookie must not leak into s2


def test_session_headers_merge_over_client_defaults(httpserver):
    def echo(request):
        return Response(request.headers.get("x-app", "none"))

    httpserver.expect_request("/echo").respond_with_handler(echo)
    with WebClient(default_headers={"x-app": "client"}) as wc:
        session = wc.session(headers={"x-app": "session"})
        assert (
            session.ref(httpserver.url_for("/echo")).resolve().collect().text_content
            == "session"
        )


def test_session_metadata_and_document_binding(httpserver, wc):
    httpserver.expect_request("/a").respond_with_data("ok")
    session = wc.session(ttl=60)
    assert session.status == "running"
    assert session.expires_at is not None
    doc = session.ref(httpserver.url_for("/a")).resolve().collect()
    assert doc.session_id == session.id
    assert doc.events[0].session_id == session.id


def test_expired_session_refuses(httpserver, wc):
    httpserver.expect_request("/a").respond_with_data("ok")
    session = wc.session(ttl=0.01)
    time.sleep(0.05)
    with pytest.raises(RuntimeError, match="expired"):
        session.ref(httpserver.url_for("/a")).resolve().collect()
    assert session.status == "expired"


def test_closed_session_refuses_and_client_close_closes_sessions(httpserver):
    wc = WebClient()
    session = wc.session()
    session.close()
    with pytest.raises(RuntimeError, match="closed"):
        session.ref(httpserver.url_for("/a")).resolve().collect()
    other = wc.session()
    wc.close()
    assert other.status == "closed"
