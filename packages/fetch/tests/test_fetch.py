"""web.fetch tests -- the transport layer in isolation, against a local httpserver."""

from __future__ import annotations

import asyncio

from pytest_httpserver import HTTPServer

from web.fetch import Fetcher, HttpFetcher, Request, Snapshot


def _run(coro):  # tiny helper: no pytest-asyncio dependency
    return asyncio.run(coro)


def test_httpfetcher_returns_a_snapshot_of_raw_bytes(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/page").respond_with_data(
        b"<h1>hi</h1>", content_type="text/html; charset=utf-8", headers={"x-test": "1"}
    )

    async def go() -> Snapshot:
        f = HttpFetcher()
        try:
            return await f.fetch(Request(url=httpserver.url_for("/page")))
        finally:
            await f.aclose()

    snap = _run(go())
    assert isinstance(snap, Snapshot) and snap.ok
    assert snap.status == 200 and snap.content == b"<h1>hi</h1>"
    assert snap.headers["x-test"] == "1"  # transport metadata preserved
    assert snap.request.url.endswith("/page") and snap.error is None
    # fetch does NOT sniff/decode -- content is raw bytes, no 'kind'/'encoding' field
    assert not hasattr(snap, "kind") and not hasattr(snap, "encoding")


def test_httpfetcher_is_a_fetcher() -> None:
    assert isinstance(HttpFetcher(), Fetcher)  # runtime-checkable interface


def test_transport_failure_is_error_not_raise() -> None:
    async def go() -> Snapshot:
        f = HttpFetcher()
        try:  # nothing is listening on this port
            return await f.fetch(Request(url="http://127.0.0.1:9/nope", timeout=0.5))
        finally:
            await f.aclose()

    snap = _run(go())
    assert not snap.ok and snap.status == 0
    assert snap.error is not None and snap.error.code == "fetch.connect"


def test_non_2xx_is_a_valid_snapshot_not_an_error(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/missing").respond_with_data(b"nope", status=404)

    async def go() -> Snapshot:
        f = HttpFetcher()
        try:
            return await f.fetch(Request(url=httpserver.url_for("/missing")))
        finally:
            await f.aclose()

    snap = _run(go())
    # a 404 is a fact the transport reports, not a transport error -- policy is resolve's call
    assert snap.status == 404 and snap.error is None and not snap.ok
    assert snap.content == b"nope"


# -- browser transport: renders JS (what a static fetch cannot) + live-page actions --

from web.fetch import BrowserFetcher, LivePage  # noqa: E402


def test_browser_executes_js_a_static_fetch_cannot(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/j").respond_with_data(
        b"<html><body><div id='root'></div>"
        b"<script>document.getElementById('root').textContent='REND'+'ERED'</script></body></html>",
        content_type="text/html")  # the string is BUILT at runtime -> not literally in the source

    async def go() -> tuple[bytes, bytes]:
        static = await HttpFetcher().fetch(Request(url=httpserver.url_for("/j")))
        bf = BrowserFetcher()
        try:
            rendered = await bf.fetch(Request(url=httpserver.url_for("/j")))
        finally:
            await bf.aclose()
        return static.content, rendered.content

    static_c, rendered_c = _run(go())
    assert b"RENDERED" not in static_c  # httpx sees the empty shell
    assert b"RENDERED" in rendered_c    # the browser ran the script


def test_live_page_actions_return_self_and_snapshot(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/f").respond_with_data(
        b"<html><body><input id='q'>"
        b"<button id='go' onclick=\"document.body.setAttribute('data-done', document.getElementById('q').value)\">go</button>"
        b"</body></html>", content_type="text/html")

    async def go() -> bytes:
        bf = BrowserFetcher()
        try:
            page = await bf.open(Request(url=httpserver.url_for("/f")))
            driven = await (await page.type("#q", "hello")).click("#go")  # actions return Self
            assert isinstance(driven, LivePage)
            snap = await driven.snapshot()
            await page.close()
            return snap.content
        finally:
            await bf.aclose()

    assert b'data-done="hello"' in _run(go())


# -- fetch-level capture: DOM events (Script recorder) + Network events on the Snapshot --

from web.fetch import DOMEvent, NetworkEvent  # noqa: E402


def test_browser_fetch_captures_network_events(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/n").respond_with_data(b"<html><body>hi</body></html>", content_type="text/html")

    async def go() -> list[NetworkEvent]:
        bf = BrowserFetcher()
        try:
            snap = await bf.fetch(Request(url=httpserver.url_for("/n")))
        finally:
            await bf.aclose()
        return [e for e in snap.events if isinstance(e, NetworkEvent)]

    nets = _run(go())
    assert any(e.url.endswith("/n") and e.status == 200 for e in nets)  # the main navigation captured


def test_actions_are_recorded_as_dom_events(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/d").respond_with_data(
        b"<html><body><button id='go' onclick=\"document.body.appendChild(document.createElement('p'))\">go</button></body></html>",
        content_type="text/html")

    async def go() -> list[DOMEvent]:
        bf = BrowserFetcher()  # default DOM_RECORDER installed
        try:
            page = await bf.open(Request(url=httpserver.url_for("/d")))
            await page.click("#go")  # mutates the DOM -> the recorder buffers it
            snap = await page.snapshot()
            await page.close()
        finally:
            await bf.aclose()
        return [e for e in snap.events if isinstance(e, DOMEvent)]

    doms = _run(go())
    assert doms and any(r["type"] == "childList" and r["added"] >= 1 for e in doms for r in e.records)


def test_http_fingerprint_sends_browser_headers(httpserver: HTTPServer) -> None:
    seen = {}
    def echo(req):
        from werkzeug.wrappers import Response
        seen["ua"] = req.headers.get("User-Agent", "")
        return Response(b"ok", content_type="text/html")
    httpserver.expect_request("/f").respond_with_handler(echo)

    async def go(fingerprint: bool) -> str:
        f = HttpFetcher(fingerprint=fingerprint)
        try:
            await f.fetch(Request(url=httpserver.url_for("/f")))
            return seen["ua"]
        finally:
            await f.aclose()

    assert "Chrome" in _run(go(True))       # fingerprint backend sends a browser UA
    assert "Chrome" not in _run(go(False))  # plain backend does not


# -- replay: record a live run's network stream, re-serve it offline (no HAR) --

from web.fetch import Recorder, ReplayBackend  # noqa: E402
from web.fetch import NetworkEvent  # noqa: E402
from web.kernel import Trace  # noqa: E402


def test_record_then_replay_offline(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/p").respond_with_data(b"<h1>recorded</h1>", content_type="text/html")

    async def record() -> list[NetworkEvent]:
        rec = Recorder(HttpFetcher())
        try:
            with Trace() as t:
                await rec.fetch(Request(url=httpserver.url_for("/p")))
            return [e for e in t.events if isinstance(e, NetworkEvent)]
        finally:
            await rec.aclose()

    events = _run(record())
    assert events and events[0].body == b"<h1>recorded</h1>"

    async def replay() -> tuple[bytes, int]:
        rb = ReplayBackend(events)
        hit = await rb.fetch(Request(url=httpserver.url_for("/p")))     # served from the recording
        miss = await rb.fetch(Request(url="https://drifted.example/x"))  # not recorded -> drift
        return hit.content, miss.status

    content, miss_status = _run(replay())
    assert content == b"<h1>recorded</h1>"  # offline, deterministic (no second server hit)
    assert miss_status == 599  # drift is visible, not a silent real fetch


def test_replay_backend_is_a_fetcher() -> None:
    from web.fetch import Fetcher
    assert isinstance(ReplayBackend([]), Fetcher) and isinstance(Recorder(HttpFetcher()), Fetcher)


def test_http_backend_classifies_failure_modes() -> None:
    async def go() -> dict[str, str]:
        f = HttpFetcher()
        out = {}
        try:  # each distinct failure -> a distinct, stable code (never raises)
            out["url"] = (await f.fetch(Request(url="ftp://nope/x"))).error.code            # unsupported scheme
            out["dns"] = (await f.fetch(Request(url="http://no.such.host.invalid/x", timeout=2.0))).error.code
            out["connect"] = (await f.fetch(Request(url="http://127.0.0.1:9/x", timeout=2.0))).error.code
        finally:
            await f.aclose()
        return out

    codes = _run(go())
    assert codes == {"url": "fetch.url", "dns": "fetch.dns", "connect": "fetch.connect"}


def test_browser_backend_never_raises_on_nav_failure() -> None:
    async def go() -> Snapshot:
        bf = BrowserFetcher()
        try:  # nothing is listening -> nav fails; must become snapshot.error, not an exception
            return await bf.fetch(Request(url="http://127.0.0.1:49999/x", timeout=3.0))
        finally:
            await bf.aclose()

    snap = _run(go())
    assert not snap.ok and snap.error is not None and snap.error.code in ("fetch.connect", "fetch.dns")
