"""web.resolve tests -- orchestration + middleware, against a local httpserver and stub fetchers."""

from __future__ import annotations

import asyncio

from pytest_httpserver import HTTPServer
from web.fetch import HttpFetcher, Request, Snapshot
from web.parse import Document
from web.resolve import RatePolicy, Resolver, RotationPolicy, rate_limit, retry


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


def test_snapshot_returns_the_raw_unparsed_snapshot(httpserver: HTTPServer) -> None:
    # snapshot() is what resolve() discards: the raw bytes + captured events, unparsed and
    # (unlike resolve) never raising on a transport failure -- Locate mines it for the XHR stream.
    httpserver.expect_request("/p").respond_with_data(
        b'{"ok":true}', content_type="application/json"
    )

    async def go() -> Snapshot:
        r = Resolver()
        try:
            return await r.snapshot(httpserver.url_for("/p"))
        finally:
            await r.aclose()

    snap = _run(go())
    assert snap.error is None and snap.ok and snap.content == b'{"ok":true}'


def test_snapshot_does_not_raise_on_transport_failure() -> None:
    # a resolver that raises on resolve() still hands back an errored Snapshot from snapshot().
    async def go() -> Snapshot:
        r = Resolver()
        try:
            return await r.snapshot(Request(url="http://127.0.0.1:1/nope"))
        finally:
            await r.aclose()

    snap = _run(go())
    assert snap.error is not None and not snap.ok


class _FlakyFetcher:
    """Fails (transport error) for the first ``fail`` calls, then succeeds -- to exercise retry."""

    def __init__(self, fail: int) -> None:
        self.calls = 0
        self._fail = fail

    async def fetch(self, request: Request) -> Snapshot:
        self.calls += 1
        if self.calls <= self._fail:
            from web.fetch import err

            return Snapshot(request=request, error=err("fetch.transport", "boom"))
        return Snapshot(
            request=request,
            status=200,
            content=b"<p>ok</p>",
            headers={"content-type": "text/html"},
        )

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


def test_retry_gives_up_then_the_policy_decides() -> None:
    import pytest
    from web.fetch import WebException

    # DEFAULT: a transport failure raises AFTER the retry middleware has run (obeying it first)
    async def raises() -> Document:
        r = Resolver(ladder=(_FlakyFetcher(fail=99),), retry=retry(max_attempts=2, backoff=0.0))
        return await r.resolve(Request(url="https://x/"))

    with pytest.raises(WebException):
        _run(raises())

    # POLICY opt-out: raise_on_error=False returns the not-ok (empty) Document instead
    fetcher = _FlakyFetcher(fail=99)

    async def returns() -> Document:
        r = Resolver(
            ladder=(fetcher,),
            retry=retry(max_attempts=2, backoff=0.0),
            raise_on_error=False,
        )
        return await r.resolve(Request(url="https://x/"))

    doc = _run(returns())
    assert fetcher.calls == 2 and doc.select("p") is None  # gave up -> empty content, no raise


def test_rate_limit_spaces_same_host_requests() -> None:
    fetcher = _FlakyFetcher(fail=0)

    async def go() -> float:
        r = Resolver(ladder=(fetcher,), rate=RatePolicy(per_host=0.05))
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await r.resolve(Request(url="https://x/a"))
        await r.resolve(Request(url="https://x/b"))
        return loop.time() - t0

    elapsed = _run(go())
    assert elapsed >= 0.05  # the second request waited for the host's slot


# -- signals: clean, standalone detector functions over a Document (a sub-part of resolve) --

from web.parse import parse  # noqa: E402
from web.resolve import (  # noqa: E402
    Signal,
    consent_wall,
    iframe,
    infinite_scroll,
    js_challenge,
    login_wall,
    pagination,
    server_error,
    spa,
)


def _d(html: bytes):  # a parsed Document from bytes
    return parse(html, content_type="text/html")


def test_spa_signal_on_a_client_rendered_shell() -> None:
    s = spa(_d(b"<html><body><div id='root'></div><script src='/app.js'></script></body></html>"))
    assert isinstance(s, Signal) and s.name == "spa"
    # a server-rendered page with real text does NOT fire spa
    assert (
        spa(_d(b"<html><body><div id='root'>" + b"content " * 60 + b"</div></body></html>")) is None
    )


def test_login_and_pagination_detectors() -> None:
    assert login_wall(_d(b"<form><input type='password'></form>")) is not None
    assert pagination(_d(b"<a rel='next' href='/2'>next</a>")) is not None


def test_consent_scroll_iframe_and_server_error_detectors() -> None:
    assert (
        consent_wall(_d(b"<body><div id='cookie-consent'>Accept cookies</div></body>")) is not None
    )
    assert infinite_scroll(_d(b"<body><div class='infinite-scroll'></div></body>")) is not None
    assert iframe(_d(b"<body><iframe src='/framed'></iframe></body>")) is not None
    assert server_error(Snapshot(request=Request(url="https://x/"), status=503)) is not None
    assert server_error(Snapshot(request=Request(url="https://x/"), status=200)) is None


def test_js_challenge_reads_wall_copy() -> None:
    # a content bot-wall ("verify you are human") is a JS/fingerprint verdict -> js_challenge
    assert (
        js_challenge(_d(b"<html><body>Please verify you are human to continue</body></html>"))
        is not None
    )
    assert (
        js_challenge(_d(b"<html><body>normal page with plenty of real content here</body></html>"))
        is None
    )


def test_trace_captures_events_across_layers(httpserver: HTTPServer) -> None:
    from web.fetch import Trace

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
from web.resolve import paginate_cursor  # noqa: E402
from web.resolve import Flag, flags  # noqa: E402


def test_flags_roll_signals_into_conclusions_with_remedies() -> None:
    doc = parse(
        b"<html><body><form><input type=password></form></body></html>",
        content_type="text/html",
    )
    fs = flags(doc)
    by_name = {f.name: f for f in fs}
    assert "auth_required" in by_name
    assert by_name["auth_required"].present and by_name["auth_required"].remedy == "session:login"
    assert all(isinstance(f, Flag) for f in fs)


def test_flags_use_the_snapshot_for_transport_conclusions() -> None:
    doc = parse(
        b"<html><body>ok content here plenty of text to not look empty at all</body></html>",
        content_type="text/html",
    )
    snap = Snapshot(
        request=Request(url="https://x/"),
        url="https://x/",
        status=429,
        headers={"content-type": "text/html"},
        content=doc.content,
    )
    by = {f.name: f for f in flags(doc, snap)}
    assert "rate_limited" in by  # 429 -> rate_limited tier
    assert by["rate_limited"].remedy == "retry:backoff"  # back off, don't climb the ladder


def test_flags_noisy_or_combines_independent_evidence() -> None:
    # a thin JS-mount shell fires BOTH spa and empty -> needs_browser combines them by noisy-OR,
    # so the conclusion is more confident than either single piece of evidence.
    doc = parse(
        b"<html><body><div id='root'></div><script src='/app.js'></script></body></html>",
        content_type="text/html",
    )
    nb = next(f for f in flags(doc) if f.name == "needs_browser")
    assert nb.confidence > 0.8 and len(nb.signals) == 2


def _snap403(doc: "Document") -> Snapshot:
    return Snapshot(
        request=Request(url="https://x/"), url="https://x/", status=403, content=doc.content
    )


def test_anti_bot_tiers_route_to_distinct_remedies() -> None:
    # a JS challenge interstitial (403 that CARRIES the challenge) -> js_challenge -> climb realness,
    # and its contra suppresses ip_blocked: a challenge is a fingerprint verdict, not an IP one.
    chal = parse(
        b"<html><body><h1>Just a moment...</h1><p>Checking your browser before you continue.</p></body></html>",
        content_type="text/html",
    )
    by = {f.name: f for f in flags(chal, _snap403(chal))}
    assert by["js_challenge"].remedy == "escalate:realness"
    assert "ip_blocked" not in by

    # a bare 403 deny with no challenge served -> ip_blocked -> escalate to a proxy/residential IP.
    bare = parse(b"<html><body>Access is denied.</body></html>", content_type="text/html")
    by2 = {f.name: f for f in flags(bare, _snap403(bare))}
    assert by2["ip_blocked"].remedy == "escalate:proxy"

    # a visible CAPTCHA puzzle -> captcha -> needs a solver, not a tier climb.
    cap = parse(
        b"<html><body><h1>Verify</h1><p>Select all images with a bus. I'm not a robot.</p></body></html>",
        content_type="text/html",
    )
    by3 = {f.name: f for f in flags(cap)}
    assert by3["captcha"].remedy == "solve:captcha"


def _page(status: int, body: bytes) -> Snapshot:
    return Snapshot(
        request=Request(url="https://x/"),
        url="https://x/",
        status=status,
        headers={"content-type": "text/html"},
        content=body,
    )


def test_transport_remedy_maps_flags_to_the_next_move() -> None:
    from web.resolve import transport_remedy

    # a JS challenge -> climb browser realness; a bare 403 deny -> a residential IP; a 429 -> back off
    assert (
        transport_remedy(
            _page(403, b"<html><body>Just a moment... Checking your browser</body></html>")
        )
        == "escalate:realness"
    )
    assert (
        transport_remedy(_page(403, b"<html><body>Access is denied.</body></html>"))
        == "escalate:proxy"
    )
    assert (
        transport_remedy(_page(429, b"<html><body>Too many requests, slow down.</body></html>"))
        == "retry:backoff"
    )
    # a clean, server-rendered page has no transport remedy
    clean = b"<html><body><p>A normal article with plenty of real readable content and no anti-bot wall on it.</p></body></html>"
    assert transport_remedy(_page(200, clean)) is None


def test_escalate_climbs_on_a_challenge_but_not_a_rate_limit() -> None:
    from web.resolve.middleware import escalate

    class _Tier:
        def __init__(self) -> None:
            self.calls = 0

        async def fetch(self, request: Request) -> Snapshot:
            self.calls += 1
            return _page(200, b"<html><body>the real content, plenty of it now</body></html>")

        async def aclose(self) -> None:
            pass

    async def run(base: Snapshot) -> "tuple[int, int]":
        tier = _Tier()

        async def nxt(_request: Request) -> Snapshot:
            return base

        out = await escalate((tier,))(Request(url="https://x/"), nxt)
        return tier.calls, out.status

    # a JS-challenge base -> the ladder climbs to the stronger tier and clears it
    calls, status = _run(run(_page(403, b"<html>Just a moment... Checking your browser</html>")))
    assert calls == 1 and status == 200
    # a 429 rate-limit base -> back off (retry's job), the ladder does NOT climb
    calls2, _ = _run(run(_page(429, b"<html>Too many requests</html>")))
    assert calls2 == 0


def test_escalate_is_sticky_per_domain() -> None:
    # domain stickiness: once a host needs the climb tier, the next same-host request STARTS there
    # instead of re-fetching (and re-failing) the base -- a crawl pays a domain's climb once.
    from web.resolve.middleware import escalate

    class _Tier:
        def __init__(self) -> None:
            self.calls = 0

        async def fetch(self, request: Request) -> Snapshot:
            self.calls += 1
            return _page(200, b"<html><body>the real content, plenty of it now</body></html>")

        async def aclose(self) -> None:
            pass

    async def go() -> "tuple[int, int]":
        tier = _Tier()
        base_calls = {"n": 0}

        async def nxt(_request: Request) -> Snapshot:  # a JS-challenge base -- always blocked
            base_calls["n"] += 1
            return _page(403, b"<html>Just a moment... Checking your browser</html>")

        mw = escalate((tier,))  # ONE middleware instance keeps the per-host memory
        req = Request(url="https://acme.example/a")
        await mw(req, nxt)  # 1st: base (blocked) -> climb to the tier
        await mw(Request(url="https://acme.example/b"), nxt)  # 2nd same host: start at the tier
        return base_calls["n"], tier.calls

    base_calls, tier_calls = _run(go())
    assert base_calls == 1  # the base was fetched only on the FIRST request; the 2nd skipped it
    assert tier_calls == 2  # both requests were served by the climb tier


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
        r = Resolver(
            paginate=paginate_cursor(cursor_path="next", param="cursor", items_path="items")
        )
        try:
            return await r.resolve(Request(url=httpserver.url_for("/api")))
        finally:
            await r.aclose()

    doc = _run(go())
    assert doc.json() == [1, 2, 3, 4, 5]  # all pages' items concatenated into one array


# -- expanded signal/flag breadth --
from web.resolve import data_api, record_list, structured_data, tabbed  # noqa: E402


def test_structured_data_from_jsonld() -> None:
    doc = parse(
        b'<html><head><script type="application/ld+json">{"@type":"Product"}</script></head><body>x</body></html>',
        content_type="text/html",
    )
    assert structured_data(doc) is not None
    assert "structured_data" in {f.name for f in flags(doc)}


def test_data_api_json_island() -> None:
    doc = parse(
        b'<html><body><script id="__NEXT_DATA__" type="application/json">{"props":{}}</script></body></html>',
        content_type="text/html",
    )
    assert data_api(doc) is not None
    by = {f.name: f for f in flags(doc)}
    assert by["data_api"].remedy == "extract:json_island"


def test_record_list_signal_carries_selector_and_count() -> None:
    html = (
        b"<html><body><ul>"
        + b"".join(b"<li class=item><span class=t>x</span></li>" for _ in range(5))
        + b"</ul></body></html>"
    )
    doc = parse(html, content_type="text/html")
    sig = record_list(doc)
    assert sig is not None
    assert sig.detail["item_selector"] == "li.item" and sig.detail["count"] == 5
    rec_flag = next(f for f in flags(doc) if f.name == "record_list")
    assert rec_flag.remedy == "extract:records"


def test_tabbed_widget() -> None:
    doc = parse(
        b'<html><body><div role="tablist"><button role="tab">A</button></div></body></html>',
        content_type="text/html",
    )
    assert tabbed(doc) is not None
    assert "tabbed" in {f.name for f in flags(doc)}


def test_retry_retries_real_transient_errors_not_persistent_ones() -> None:
    from web.fetch import err
    from web.resolve.middleware import _retriable

    # the common transient transport errors must be retried (previously only "fetch.transport" was)
    for code in (
        "fetch.timeout",
        "fetch.connect",
        "fetch.dns",
        "fetch.proxy",
        "fetch.transport",
    ):
        assert _retriable(Snapshot(request=Request(url="https://x/"), error=err(code, "x"))) is True
    # persistent errors must NOT be retried (a retry can't help)
    for code in ("fetch.tls", "fetch.url", "fetch.redirects"):
        assert (
            _retriable(Snapshot(request=Request(url="https://x/"), error=err(code, "x"))) is False
        )


def test_paginate_param_aggregates_rows_across_full_html_pages(
    httpserver: HTTPServer,
) -> None:
    # each page is a FULL <html> document; the merged Document must span ALL pages (regression:
    # concatenating whole docs kept only page 1's rows)
    def handler(req):
        from werkzeug.wrappers import Response

        page = int(req.args.get("page", "1"))
        if page > 3:
            return Response(b"<html><body><ul></ul></body></html>", content_type="text/html")
        items = "".join(f"<li class=row>p{page}-{i}</li>" for i in range(2))
        return Response(
            f"<html><body><ul>{items}</ul></body></html>".encode(),
            content_type="text/html",
        )

    httpserver.expect_request("/list").respond_with_handler(handler)

    async def go() -> list[str]:
        from web.resolve import paginate_param, until_empty

        r = Resolver(paginate=paginate_param("page", until=until_empty("li.row"), max_pages=5))
        try:
            doc = await r.resolve(Request(url=httpserver.url_for("/list")))
            return [e.text for e in doc.select_all("li.row")]
        finally:
            await r.aclose()

    rows = _run(go())
    assert rows == [
        "p1-0",
        "p1-1",
        "p2-0",
        "p2-1",
        "p3-0",
        "p3-1",
    ]  # all 3 pages aggregated


def test_rotate_middleware_presents_fleet_identities(httpserver: HTTPServer) -> None:
    from web.fetch import ClientPool, Fingerprint

    httpserver.expect_request("/").respond_with_data(b"<html></html>", content_type="text/html")
    fp1 = Fingerprint(user_agent="Agent/1")
    fp2 = Fingerprint(user_agent="Agent/2")

    async def go() -> None:
        async with ClientPool() as pool:  # rotation re-leases a fresh-identity backend per request
            rs = Resolver(rotate=RotationPolicy(fleet=(fp1, fp2)), pool=pool)
            for _ in range(6):
                await rs.resolve(Request(url=httpserver.url_for("/")))

    _run(go())
    seen = {req.headers.get("User-Agent", "") for req, _ in httpserver.log}
    assert seen and seen <= {
        "Agent/1",
        "Agent/2",
    }  # every request presented a fleet identity


def test_policies_are_serialisable_and_build_middleware() -> None:
    from web.fetch import ClientPool
    from web.fetch import profiles as fp
    from web.resolve import EscalationPolicy, Profile, RatePolicy, RetryPolicy

    # a policy is a plain (pydantic) model -> a Profile bundling them is fully serialisable
    prof = Profile(
        escalation=EscalationPolicy(tiers=(fp.BASIC, fp.BROWSER), on=("403",)),
        retry=RetryPolicy(max_attempts=5),
        rate=RatePolicy(per_host=1.0),
    )
    assert '"max_attempts":5' in prof.retry.model_dump_json()  # type: ignore[union-attr]
    assert prof.escalation is not None and prof.escalation.on == ("403",)

    async def go() -> bool:
        async with ClientPool() as pool:  # .build(pool) turns a policy into its middleware
            return callable(RetryPolicy(max_attempts=2).build(pool))

    assert _run(go())
