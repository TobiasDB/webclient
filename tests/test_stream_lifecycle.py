"""Regression tests for the stream bridge leak: a `_pump` task must never
survive its consumer, however the consumer stops (fully drained, broken
early, abandoned, or the client closed mid-stream)."""
import gc

import pytest

from webclient import WebClient, q


def stranded_streams(wc):
    """Count still-running stream-pump tasks on the client's loop."""
    import asyncio

    async def scan():
        return sum(
            1 for t in asyncio.all_tasks()
            if t.get_coro().__qualname__.endswith("stream.<locals>._pump")
            and not t.done())

    return wc._ensure_loop().run(scan())


def serve_chain(httpserver, count=5):
    for n in range(1, count + 1):
        nxt = (f'<a class="next" href="/p{n + 1}">n</a>' if n < count else "")
        httpserver.expect_request(f"/p{n}").respond_with_data(
            f"<html><body><h1>{n}</h1>{nxt}</body></html>",
            content_type="text/html")


def test_fully_drained_stream_leaves_no_pump(httpserver):
    serve_chain(httpserver)
    with WebClient() as wc:
        first = wc.ref(httpserver.url_for("/p1")).fetch()
        list(first.paginate("a.next"))          # drain fully
        assert stranded_streams(wc) == 0


def test_broken_stream_leaves_no_pump(httpserver):
    serve_chain(httpserver)
    with WebClient() as wc:
        first = wc.ref(httpserver.url_for("/p1")).fetch()
        pages = first.paginate("a.next")
        for i, _ in enumerate(pages):
            if i == 1:
                break                            # abandon mid-stream
        pages.close()                            # explicit close runs finally
        assert stranded_streams(wc) == 0


def test_abandoned_stream_generator_cleans_up(httpserver):
    serve_chain(httpserver)
    with WebClient() as wc:
        first = wc.ref(httpserver.url_for("/p1")).fetch()
        it = first.paginate("a.next")
        next(it)
        del it                                   # drop without closing
        gc.collect()
        assert stranded_streams(wc) == 0


def test_close_client_mid_stream_is_clean(httpserver):
    serve_chain(httpserver)
    wc = WebClient()
    first = wc.ref(httpserver.url_for("/p1")).fetch()
    pages = first.paginate("a.next")
    next(pages)                                  # start but don't finish
    wc.close()                                   # must not strand the pump
    # consuming further after close stops cleanly rather than hanging
    assert list(pages) == []


def test_stream_surfaces_producer_errors(httpserver):
    httpserver.expect_request("/bad").respond_with_data(
        "<html><body>no next link here</body></html>", content_type="text/html")
    with WebClient() as wc:
        first = wc.ref(httpserver.url_for("/bad")).fetch()

        def boom(_doc):
            raise ValueError("driver blew up")

        with pytest.raises(ValueError, match="blew up"):
            list(first.paginate(boom))
        assert stranded_streams(wc) == 0
