import pytest

from webclient import Reference, WebClient


@pytest.fixture
def wc():
    with WebClient() as client:
        yield client


def serve_chain(httpserver, count=4, stop_marker_on=None):
    """/p1 -> /p2 -> ... -> /p<count> (last has no next link)."""
    for n in range(1, count + 1):
        next_link = (f'<a class="next" href="/p{n + 1}">next</a>'
                     if n < count else "")
        marker = '<div class="stop"></div>' if n == stop_marker_on else ""
        httpserver.expect_request(f"/p{n}").respond_with_data(
            f'<html><body><h1>page {n}</h1>{marker}{next_link}</body></html>',
            content_type="text/html")


def titles(pages):
    return [doc.select("h1").text for doc in pages]


def test_paginate_by_selector(httpserver, wc):
    serve_chain(httpserver)
    first = wc.ref(httpserver.url_for("/p1")).fetch()
    assert titles(first.paginate("a.next")) == [
        "page 1", "page 2", "page 3", "page 4"]


def test_paginate_limit_and_offset(httpserver, wc):
    serve_chain(httpserver)
    first = wc.ref(httpserver.url_for("/p1")).fetch()
    assert titles(first.paginate("a.next", limit=2)) == ["page 1", "page 2"]
    first = wc.ref(httpserver.url_for("/p1")).fetch()
    assert titles(first.paginate("a.next", offset=2)) == ["page 3", "page 4"]


def test_paginate_until_selector_stops_before_yielding(httpserver, wc):
    serve_chain(httpserver, stop_marker_on=3)
    first = wc.ref(httpserver.url_for("/p1")).fetch()
    assert titles(first.paginate("a.next", until=".stop")) == [
        "page 1", "page 2"]


def test_paginate_until_predicate(httpserver, wc):
    serve_chain(httpserver)
    first = wc.ref(httpserver.url_for("/p1")).fetch()
    pages = first.paginate(
        "a.next", until=lambda d: "page 3" in d.select("h1").text)
    assert titles(pages) == ["page 1", "page 2"]


def test_paginate_by_callable(httpserver, wc):
    serve_chain(httpserver)

    def next_ref(doc):
        node = doc.select("a.next", optional=True)
        return node.attr("href", optional=True) if node else None

    first = wc.ref(httpserver.url_for("/p1")).fetch()
    assert len(titles(first.paginate(next_ref))) == 4


def test_paginate_by_iterable_of_params(httpserver, wc):
    def handler(request):
        from werkzeug.wrappers import Response
        page = request.args.get("page", "1")
        return Response(f"<h1>page {page}</h1>", content_type="text/html")

    httpserver.expect_request("/list").respond_with_handler(handler)
    first = wc.ref(httpserver.url_for("/list")).fetch()
    pages = first.paginate([{"page": n} for n in range(2, 5)])
    assert titles(pages) == ["page 1", "page 2", "page 3", "page 4"]


def test_paginate_resume(httpserver, wc):
    serve_chain(httpserver)
    first = wc.ref(httpserver.url_for("/p1")).fetch()
    resume_ref = Reference.from_url(httpserver.url_for("/p3"))
    assert titles(first.paginate("a.next", resume=resume_ref)) == [
        "page 3", "page 4"]


def test_paginate_uses_session(httpserver):
    from werkzeug.wrappers import Response

    def handler(request):
        token = request.cookies.get("token", "none")
        return Response(f"<h1>{token}</h1>", content_type="text/html")

    httpserver.expect_request("/login").respond_with_response(
        Response("ok", headers={"Set-Cookie": "token=tok; Path=/"}))
    httpserver.expect_request("/feed").respond_with_handler(handler)

    with WebClient() as wc:
        session = wc.session()
        session.ref(httpserver.url_for("/login")).fetch()
        first = session.ref(httpserver.url_for("/feed")).fetch()
        pages = first.paginate([{"page": 2}])
        assert titles(pages) == ["tok", "tok"]
