"""web.resolve tests -- orchestration + middleware, against a local httpserver and stub fetchers."""

from __future__ import annotations

import asyncio

from pytest_httpserver import HTTPServer

from web.fetch import HttpFetcher, Request, Snapshot
from web.parse import Document
from web.resolve import Resolver, rate_limit, retry


def _run(coro):
    return asyncio.run(coro)


def test_resolver_fetches_and_parses(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/p").respond_with_data(b"<h1>hi</h1>", content_type="text/html")

    async def go() -> Document:
        r = Resolver()
        try:
            return await r.resolve(Request(url=httpserver.url_for("/p")))
        finally:
            await r.aclose()

    doc = _run(go())
    assert doc.kind == "html" and doc.select_all("h1")[0].text == "hi"


class _FlakyFetcher:
    """Fails (transport error) for the first ``fail`` calls, then succeeds -- to exercise retry."""

    def __init__(self, fail: int) -> None:
        self.calls = 0
        self._fail = fail

    async def fetch(self, request: Request) -> Snapshot:
        self.calls += 1
        if self.calls <= self._fail:
            from web.kernel import err

            return Snapshot(request=request, error=err("fetch.transport", "boom"))
        return Snapshot(request=request, status=200, content=b"<p>ok</p>",
                        headers={"content-type": "text/html"})

    async def aclose(self) -> None:
        pass


def test_retry_middleware_recovers_from_transient_failure() -> None:
    fetcher = _FlakyFetcher(fail=2)

    async def go() -> Document:
        r = Resolver(ladder=(fetcher,), retry=retry(max_attempts=3, backoff=0.0))
        return await r.resolve(Request(url="https://x/"))

    doc = _run(go())
    assert fetcher.calls == 3 and doc.select_all("p")[0].text == "ok"  # 2 failures + 1 success
    assert doc.select_all("p")[0].text == "ok"


def test_retry_gives_up_and_returns_the_last_document() -> None:
    fetcher = _FlakyFetcher(fail=99)

    async def go() -> Document:
        r = Resolver(ladder=(fetcher,), retry=retry(max_attempts=2, backoff=0.0))
        return await r.resolve(Request(url="https://x/"))

    doc = _run(go())
    assert fetcher.calls == 2 and doc.select("p") is None  # gave up -> empty content


def test_rate_limit_spaces_same_host_requests() -> None:
    fetcher = _FlakyFetcher(fail=0)

    async def go() -> float:
        r = Resolver(ladder=(fetcher,), rate_limit=0.05)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await r.resolve(Request(url="https://x/a"))
        await r.resolve(Request(url="https://x/b"))
        return loop.time() - t0

    elapsed = _run(go())
    assert elapsed >= 0.05  # the second request waited for the host's slot


# -- signals: clean, standalone detector functions over a Document (a sub-part of resolve) --

from web.parse import parse  # noqa: E402
from web.resolve import Signal, anti_bot, login_wall, pagination, spa  # noqa: E402


def _d(html: bytes):  # a parsed Document from bytes
    return parse(html, content_type="text/html")


def test_spa_signal_on_a_client_rendered_shell() -> None:
    s = spa(_d(b"<html><body><div id='root'></div><script src='/app.js'></script></body></html>"))
    assert isinstance(s, Signal) and s.name == "spa"
    # a server-rendered page with real text does NOT fire spa
    assert spa(_d(b"<html><body><div id='root'>" + b"content " * 60 + b"</div></body></html>")) is None


def test_login_and_pagination_detectors() -> None:
    assert login_wall(_d(b"<form><input type='password'></form>")) is not None
    assert pagination(_d(b"<a rel='next' href='/2'>next</a>")) is not None


def test_anti_bot_reads_content_markers() -> None:
    assert anti_bot(_d(b"<html><body>Please verify you are human (captcha)</body></html>")) is not None
    assert anti_bot(_d(b"<html><body>normal page</body></html>")) is None


def test_trace_captures_events_across_layers(httpserver: HTTPServer) -> None:
    from web.kernel import Trace

    httpserver.expect_request("/p").respond_with_data(b"<h1>hi</h1>", content_type="text/html")
    fetcher = _FlakyFetcher(fail=1)  # one transient failure -> a retry event too

    async def go_flaky() -> list[str]:
        r = Resolver(ladder=(fetcher,), retry=retry(3, backoff=0.0))
        try:
            await r.resolve(Request(url="https://x/"))
        finally:
            await r.aclose()
        return []

    with Trace() as t:
        _run(go_flaky())
    topics = [e.topic for e in t.events]
    # the flaky fetcher isn't an http backend so no FetchEvent, but the retry policy emitted one
    assert "resolve" in topics and any(getattr(e, "phase", "") == "retry" for e in t.events)

    # a real http fetch emits a FetchEvent
    async def go_http() -> None:
        r = Resolver()
        try:
            await r.resolve(Request(url=httpserver.url_for("/p")))
        finally:
            await r.aclose()

    with Trace() as t2:
        _run(go_http())
    assert any(e.topic == "fetch" and getattr(e, "status", 0) == 200 for e in t2.events)


# -- flags: conclusions rolled up from signals, with remedies --
from web.resolve import Flag, flags  # noqa: E402
from web.resolve import paginate_cursor  # noqa: E402


def test_flags_roll_signals_into_conclusions_with_remedies() -> None:
    doc = parse(b"<html><body><form><input type=password></form></body></html>", content_type="text/html")
    fs = flags(doc)
    by_name = {f.name: f for f in fs}
    assert "auth_required" in by_name
    assert by_name["auth_required"].present and by_name["auth_required"].remedy == "session:login"
    assert all(isinstance(f, Flag) for f in fs)


def test_flags_use_the_snapshot_for_transport_conclusions() -> None:
    doc = parse(b"<html><body>ok content here plenty of text to not look empty at all</body></html>",
                content_type="text/html")
    snap = Snapshot(request=Request(url="https://x/"), url="https://x/", status=429,
                    headers={"content-type": "text/html"}, content=doc.content)
    names = {f.name for f in flags(doc, snap)}
    assert "blocked" in names  # 429 -> blocked_status evidence -> blocked conclusion


def test_flags_noisy_or_combines_independent_evidence() -> None:
    # anti-bot content AND a 403 status both feed "blocked" -> combined confidence exceeds either
    doc = parse(b"<html><body>Please verify you are human to continue</body></html>", content_type="text/html")
    snap = Snapshot(request=Request(url="https://x/"), url="https://x/", status=403, content=doc.content)
    blocked = next(f for f in flags(doc, snap) if f.name == "blocked")
    assert blocked.confidence > 0.9 and len(blocked.signals) == 2


def test_paginate_cursor_concatenates_json_pages(httpserver: HTTPServer) -> None:
    import json
    def handler(req):
        from werkzeug.wrappers import Response
        cur = req.args.get("cursor")
        if cur is None:
            body = {"items": [1, 2], "next": "abc"}
        elif cur == "abc":
            body = {"items": [3, 4], "next": "def"}
        else:
            body = {"items": [5], "next": None}
        return Response(json.dumps(body), content_type="application/json")
    httpserver.expect_request("/api").respond_with_handler(handler)

    async def go() -> Document:
        r = Resolver(paginate=paginate_cursor(cursor_path="next", param="cursor", items_path="items"))
        try:
            return await r.resolve(Request(url=httpserver.url_for("/api")))
        finally:
            await r.aclose()

    doc = _run(go())
    assert doc.json() == [1, 2, 3, 4, 5]  # all pages' items concatenated into one array


# -- expanded signal/flag breadth --
from web.resolve import data_api, record_list, structured_data, tabbed  # noqa: E402


def test_structured_data_from_jsonld() -> None:
    doc = parse(b'<html><head><script type="application/ld+json">{"@type":"Product"}</script></head><body>x</body></html>',
                content_type="text/html")
    assert structured_data(doc) is not None
    assert "structured_data" in {f.name for f in flags(doc)}


def test_data_api_json_island() -> None:
    doc = parse(b'<html><body><script id="__NEXT_DATA__" type="application/json">{"props":{}}</script></body></html>',
                content_type="text/html")
    assert data_api(doc) is not None
    by = {f.name: f for f in flags(doc)}
    assert by["data_api"].remedy == "extract:json_island"


def test_record_list_signal_carries_selector_and_count() -> None:
    html = (b"<html><body><ul>"
            + b"".join(b"<li class=item><span class=t>x</span></li>" for _ in range(5))
            + b"</ul></body></html>")
    doc = parse(html, content_type="text/html")
    sig = record_list(doc)
    assert sig is not None
    assert sig.detail["item_selector"] == "li.item" and sig.detail["count"] == 5
    rec_flag = next(f for f in flags(doc) if f.name == "record_list")
    assert rec_flag.remedy == "extract:records"


def test_tabbed_widget() -> None:
    doc = parse(b'<html><body><div role="tablist"><button role="tab">A</button></div></body></html>',
                content_type="text/html")
    assert tabbed(doc) is not None
    assert "tabbed" in {f.name for f in flags(doc)}


def test_retry_retries_real_transient_errors_not_persistent_ones() -> None:
    from web.kernel import err
    from web.resolve.middleware import _retriable

    # the common transient transport errors must be retried (previously only "fetch.transport" was)
    for code in ("fetch.timeout", "fetch.connect", "fetch.dns", "fetch.proxy", "fetch.transport"):
        assert _retriable(Snapshot(request=Request(url="https://x/"), error=err(code, "x"))) is True
    # persistent errors must NOT be retried (a retry can't help)
    for code in ("fetch.tls", "fetch.url", "fetch.redirects"):
        assert _retriable(Snapshot(request=Request(url="https://x/"), error=err(code, "x"))) is False
