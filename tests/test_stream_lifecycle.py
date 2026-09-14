"""Regression tests for the stream bridge leak: a `_pump` task must never
survive its consumer, however the consumer stops (fully drained, broken
early, abandoned, or the client closed mid-stream). The vehicle is now a
streamed plan (pagination is gone)."""

import gc

import pytest

from webclient import WebClient, doc, ref

CARDS = (
    "<html><body>"
    + "".join(f'<div class="c"><a href="/i/{n}">{n}</a></div>' for n in range(5))
    + "</body></html>"
)


def stranded_streams(wc):
    import asyncio

    async def scan():
        return sum(
            1
            for t in asyncio.all_tasks()
            if t.get_coro().__qualname__.endswith("stream.<locals>._pump")
            and not t.done()
        )

    return wc.loop().run(scan())


def plan():
    return (
        ref.resolve().select_all(".c").extract(n=doc.select("a").text_content).project()
    )


def serve(httpserver):
    httpserver.expect_request("/cards").respond_with_data(
        CARDS, content_type="text/html"
    )
    return httpserver.url_for("/cards")


def test_fully_drained_stream_leaves_no_pump(httpserver):
    url = serve(httpserver)
    with WebClient() as wc:
        list(plan().stream(wc.ref(url)))
        assert stranded_streams(wc) == 0


def test_broken_stream_leaves_no_pump(httpserver):
    url = serve(httpserver)
    with WebClient() as wc:
        rows = plan().stream(wc.ref(url))
        for i, _ in enumerate(rows):
            if i == 1:
                break
        rows.close()
        assert stranded_streams(wc) == 0


def test_abandoned_stream_generator_cleans_up(httpserver):
    url = serve(httpserver)
    with WebClient() as wc:
        it = plan().stream(wc.ref(url))
        next(it)
        del it
        gc.collect()
        assert stranded_streams(wc) == 0


def test_close_client_mid_stream_is_clean(httpserver):
    url = serve(httpserver)
    wc = WebClient()
    rows = plan().stream(wc.ref(url))
    next(rows)
    wc.close()
    assert list(rows) == []
