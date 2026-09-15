"""Resiliency P1: pure response classification + observe-mode probe recording.

The escalation ladder (acting on the detection) lands in later phases; here we
check detection is accurate + conservative, and that a static fetch records what
it detected onto the ``probe`` summary facet (without escalating).
"""

import pytest

from webclient import WebClient
from webclient.resiliency import classify

HTML = {"content-type": "text/html; charset=utf-8"}


def test_normal_page_detects_nothing():
    body = b"<html><body><h1>Hello</h1>" + b"real article content " * 60 + b"</body></html>"
    s = classify(200, HTML, {}, body)
    assert not s.any and not s.needs_browser


def test_cloudflare_challenge():
    s = classify(503, {**HTML, "cf-ray": "1"}, ["__cf_bm"], b"Just a moment... checking your browser")
    assert s.anti_bot == "cloudflare"


def test_datadome_block():
    s = classify(403, {**HTML, "x-datadome": "1"}, {}, b"blocked")
    assert s.anti_bot == "datadome" and s.blocked


@pytest.mark.parametrize("status", [403, 429, 503])
def test_bare_block_status_is_a_generic_challenge(status):
    # no vendor fingerprint at all -- the status code alone is anti-bot evidence.
    s = classify(status, HTML, {}, b"nope")
    assert s.anti_bot == "challenge"


def test_401_is_a_login_wall_not_an_anti_bot_challenge():
    s = classify(401, HTML, {}, b"unauthorized")
    assert s.anti_bot is None and s.login_wall


def test_ordinary_404_is_not_a_challenge():
    s = classify(404, HTML, {}, b"<html><body>not found, sorry</body></html>")
    assert s.anti_bot is None


def test_spa_shell_needs_browser():
    s = classify(200, HTML, {}, b'<html><body><div id="root"></div><script src="/a.js"></script></body></html>')
    assert s.js_required and s.needs_browser


def test_bare_empty_page_flagged_but_not_browser_worthy():
    # a truly empty page (no script) -- a browser tier would not fill it.
    s = classify(200, HTML, {}, b"<html><body></body></html>")
    assert s.empty and not s.needs_browser


def test_empty_with_script_needs_browser():
    s = classify(200, HTML, {}, b"<html><body><script src='/a.js'></script></body></html>")
    assert s.needs_browser


def test_cdn_header_on_a_normal_200_is_not_a_block():
    # cf-ray sits on *every* Cloudflare-served page -- a 200 with real content
    # must not be mistaken for a challenge.
    body = b"<html><body><h1>Fine</h1>" + b"real content " * 40 + b"</body></html>"
    s = classify(200, {**HTML, "cf-ray": "1-x"}, ["__cf_bm"], body)
    assert s.anti_bot is None and not s.any


def test_paywall_json_ld():
    body = b'<script type="application/ld+json">{"isAccessibleForFree": false}</script>' + b"x" * 300
    assert classify(200, HTML, {}, body).paywall


def test_login_wall_password_field():
    body = b"<form><input type='password'></form>" + b"content " * 50
    assert classify(200, HTML, {}, body).login_wall


def test_401_with_vendor_cookie_is_login_wall_not_anti_bot():
    # R-M6/L6: a 401 is an auth wall, not an anti-bot challenge -- a vendor cookie
    # on it must not double-label it as anti-bot.
    s = classify(401, {**HTML, "cf-ray": "1"}, ["__cf_bm"], b"unauthorized")
    assert s.anti_bot is None and s.login_wall


def test_retriable_statuses_match_the_policy():
    # L2: local retriability aligns with RetryPolicy.on_statuses (501/505 not retriable)
    from webclient.errors import error_for

    assert error_for(503).retriable and error_for(500).retriable
    assert error_for(429).retriable and error_for(0).retriable  # transport
    assert not error_for(501).retriable and not error_for(505).retriable
    assert not error_for(404).retriable


def test_login_form_in_header_is_not_a_login_wall():
    # a content page whose header has a sign-in form must NOT be flagged (R-M8):
    body = (
        b"<header><form><input type='password'></form></header>"
        b"<main>" + b"real article content here " * 80 + b"</main>"
    )
    s = classify(200, HTML, {}, body)
    assert not s.login_wall  # lots of other content -> not a wall


def test_dedicated_login_page_is_a_login_wall():
    body = b"<h1>Sign in</h1><form><input type='password'></form>"
    assert classify(200, HTML, {}, body).login_wall


def test_conservative_no_false_positives():
    # a link to /login and the word "subscribe" must NOT trip login/paywall
    body = b'<a href="/login">Sign in</a> subscribe to our newsletter ' + b"article " * 80
    s = classify(200, HTML, {}, body)
    assert not s.login_wall and not s.paywall and not s.any


# -- observe mode over a real fetch --------------------------------------------


@pytest.fixture
def wc():
    with WebClient() as client:
        yield client


def test_static_fetch_records_anti_bot_probe(httpserver, wc):
    httpserver.expect_request("/blocked").respond_with_data(
        "<html>blocked</html>",
        status=403,
        headers={"x-datadome": "1", "Content-Type": "text/html"},
    )
    doc = wc.ref(httpserver.url_for("/blocked")).resolve(error=None, optional=True).collect()
    probe = doc.probe()
    assert probe is not None and probe.anti_bot == "datadome"


def test_static_fetch_of_normal_page_has_no_probe(httpserver, wc):
    httpserver.expect_request("/ok").respond_with_data(
        "<html><body><h1>Fine</h1>" + "content " * 80 + "</body></html>",
        content_type="text/html",
    )
    doc = wc.fetch(httpserver.url_for("/ok"))
    assert not doc.has_op("probe")  # nothing to escalate -> facet absent


# -- P3/P4: resolve policy declared to a proxy service as request headers -------


def test_resolve_policy_is_sent_as_proxy_headers(httpserver):
    from werkzeug.wrappers import Response

    from webclient.core.reference.models import ProxyPolicy, RatePolicy, Resolve

    seen: dict[str, str] = {}

    def handler(request):
        seen.update({k.lower(): v for k, v in request.headers.items()})
        return Response("<html><body>ok content here</body></html>", content_type="text/html")

    httpserver.expect_request("/p").respond_with_handler(handler)
    resolve = Resolve(
        proxy=ProxyPolicy(pool="residential", geo="us"),
        rate=RatePolicy(rps=2.0, concurrency=4),
    )
    with WebClient(resolve=resolve) as wc:
        wc.fetch(httpserver.url_for("/p"))
    assert seen.get("x-webclient-proxy") == "on"
    assert seen.get("x-webclient-proxy-pool") == "residential"
    assert seen.get("x-webclient-proxy-geo") == "us"
    assert seen.get("x-webclient-rate-rps") == "2.0"
    assert seen.get("x-webclient-retry-max") == "2"  # retry always declared


def test_no_resolve_sends_no_policy_headers(httpserver):
    from werkzeug.wrappers import Response

    seen: dict[str, str] = {}

    def handler(request):
        seen.update({k.lower(): v for k, v in request.headers.items()})
        return Response("<html><body>ok content here</body></html>", content_type="text/html")

    httpserver.expect_request("/q").respond_with_handler(handler)
    with WebClient() as wc:  # no resolve bundle
        wc.fetch(httpserver.url_for("/q"))
    assert not any(k.startswith("x-webclient-") for k in seen)


# -- P2: the browser="auto" adaptive ladder (needs a real browser) -------------

_SPA = (
    "<html><body><div id='root'></div><script>"
    "document.getElementById('root').innerHTML="
    "'<h1>Loaded content here</h1>' + 'x'.repeat(300);"
    "</script></body></html>"
)


def test_browser_auto_escalates_a_js_gated_page(httpserver, wc):
    httpserver.expect_request("/spa").respond_with_data(_SPA, content_type="text/html")
    url = httpserver.url_for("/spa")
    # a plain static fetch: empty shell, records js_required, does NOT escalate
    static = wc.fetch(url)
    sp = static.probe()
    assert sp is not None and sp.js_required and not sp.was_browser_required
    # browser="auto": the JS-gated page is escalated to a browser render
    auto = wc.fetch(url, browser="auto")
    assert "Loaded content" in (auto.text_content or "")  # JS ran
    assert auto.probe().was_browser_required is True  # the facet
    assert auto._probe.escalation == ["static", "browser"]  # the raw record's trail


def test_browser_auto_stays_static_for_a_normal_page(httpserver, wc):
    httpserver.expect_request("/plain").respond_with_data(
        "<html><body><h1>Fine</h1>" + "real content " * 80 + "</body></html>",
        content_type="text/html",
    )
    doc = wc.fetch(httpserver.url_for("/plain"), browser="auto")
    assert doc._page is None  # never launched a browser
    assert not doc.has_op("probe")
