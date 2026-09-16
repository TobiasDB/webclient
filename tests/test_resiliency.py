"""Resiliency: pure request+static flag detection + the flag-driven escalation ladder.

Detection (:func:`webclient.signals.flags_from_response`) is pure and conservative; a
resolved document surfaces the conclusions through the ``flags`` facet (spa /
anti_bot_present / anti_bot_triggered / login_present / login_required / …), and
``browser="auto"`` acts on them: a login wall fails, an anti-bot challenge escalates
(proxy / stealth), a SPA escalates to a browser render.
"""

import pytest

from webclient import WebClient, WebException
from webclient.signals import flags_from_response

HTML = {"content-type": "text/html; charset=utf-8"}


def _f(status, headers, cookies, body):
    """The request/static flags of a raw response (a dict name -> Flag)."""
    return flags_from_response(status, headers, cookies, body)


def test_normal_page_detects_nothing():
    body = b"<html><body><h1>Hello</h1>" + b"real article content " * 60 + b"</body></html>"
    flags = _f(200, HTML, {}, body)
    assert not any(f.present for f in flags.values())


def test_cloudflare_challenge_is_triggered_stealth():
    f = _f(503, {**HTML, "cf-ray": "1"}, ["__cf_bm"], b"Just a moment... checking your browser")
    ab = f["anti_bot_triggered"]
    assert ab.present and ab.remedy == "stealth"  # a named vendor -> a stealth browser
    assert {s.name for s in ab.signals} >= {"challenge_interstitial", "vendor_on_block"}


def test_datadome_block_is_triggered():
    f = _f(403, {**HTML, "x-datadome": "1"}, {}, b"blocked")
    assert f["anti_bot_triggered"].present and f["anti_bot_triggered"].remedy == "stealth"


@pytest.mark.parametrize("status", [403, 429, 503])
def test_bare_block_status_is_a_triggered_proxy(status):
    # no vendor fingerprint -- the status alone triggers anti-bot; a fresh IP may help.
    f = _f(status, HTML, {}, b"nope")
    ab = f["anti_bot_triggered"]
    assert ab.present and ab.remedy == "proxy"


def test_cdn_fingerprint_on_a_200_is_present_not_triggered():
    # cf-ray + __cf_bm on a real 200 page: the vendor is in FRONT (present) but is NOT
    # challenging us (not triggered) -- the present/triggered distinction.
    body = b"<html><body><h1>Fine</h1>" + b"real content " * 40 + b"</body></html>"
    f = _f(200, {**HTML, "cf-ray": "1-x"}, ["__cf_bm"], body)
    assert f["anti_bot_present"].present and f["anti_bot_present"].value == "cloudflare"
    assert not f["anti_bot_triggered"].present


def test_401_is_a_login_wall_not_an_anti_bot_challenge():
    f = _f(401, HTML, {}, b"unauthorized")
    assert f["login_required"].present and not f["anti_bot_triggered"].present


def test_401_with_vendor_cookie_is_login_not_anti_bot():
    # a 401 is an auth wall; a vendor cookie on it is "present" but not "triggered".
    f = _f(401, {**HTML, "cf-ray": "1"}, ["__cf_bm"], b"unauthorized")
    assert f["login_required"].present and not f["anti_bot_triggered"].present


def test_ordinary_404_is_not_a_challenge():
    f = _f(404, HTML, {}, b"<html><body>not found, sorry</body></html>")
    assert not f["anti_bot_triggered"].present


def test_spa_shell_is_a_spa():
    f = _f(200, HTML, {}, b'<html><body><div id="root"></div><script src="/a.js"></script></body></html>')
    assert f["spa"].present and f["spa"].remedy == "browser"


def test_bare_empty_page_is_not_a_spa():
    # a truly empty page (no script / no shell) -- a browser tier would not fill it.
    f = _f(200, HTML, {}, b"<html><body></body></html>")
    assert not f["spa"].present


def test_empty_with_script_is_a_spa():
    f = _f(200, HTML, {}, b"<html><body><script src='/a.js'></script></body></html>")
    assert f["spa"].present


def test_framework_marker_contributes_to_spa():
    body = b'<html><head><script src="/_next/x.js"></script></head><body>hi</body></html>'
    spa = _f(200, HTML, {}, body)["spa"]
    assert any(s.name == "framework_marker" and s.value == "next" for s in spa.signals)


def test_login_present_vs_required():
    # a content page whose header has a sign-in form: login is PRESENT (a form exists)
    # but not REQUIRED (there is plenty of other content -- not a wall).
    body = (
        b"<header><form><input type='password'></form></header>"
        b"<main>" + b"real article content here " * 80 + b"</main>"
    )
    f = _f(200, HTML, {}, body)
    assert f["login_present"].present and not f["login_required"].present


def test_dedicated_login_page_is_required():
    f = _f(200, HTML, {}, b"<h1>Sign in</h1><form><input type='password'></form>")
    assert f["login_required"].present


def test_conservative_no_false_positives():
    # a link to /login + the word "subscribe" must NOT trip login_required
    body = b'<a href="/login">Sign in</a> subscribe to our newsletter ' + b"article " * 80
    f = _f(200, HTML, {}, body)
    assert not f["login_required"].present and not f["anti_bot_triggered"].present


def test_retriable_statuses_match_the_policy():
    from webclient.errors import error_for

    assert error_for(503).retriable and error_for(500).retriable
    assert error_for(429).retriable and error_for(0).retriable  # transport
    assert not error_for(501).retriable and not error_for(505).retriable
    assert not error_for(404).retriable


# -- the flags facet over a real fetch -----------------------------------------


@pytest.fixture
def wc():
    with WebClient() as client:
        yield client


def test_static_fetch_reports_the_anti_bot_flag(httpserver, wc):
    httpserver.expect_request("/blocked").respond_with_data(
        "<html>blocked</html>", status=403,
        headers={"x-datadome": "1", "Content-Type": "text/html"},
    )
    doc = wc.ref(httpserver.url_for("/blocked")).resolve(error=None, optional=True).collect()
    ab = doc.anti_bot_triggered()  # the flags facet reads the response's own status/headers
    assert ab.present and ab.remedy == "stealth"


def test_static_fetch_of_normal_page_reports_no_flags(httpserver, wc):
    httpserver.expect_request("/ok").respond_with_data(
        "<html><body><h1>Fine</h1>" + "content " * 80 + "</body></html>",
        content_type="text/html",
    )
    doc = wc.fetch(httpserver.url_for("/ok"))
    assert doc.flags() == []  # nothing notable (total facet, empty)


def test_auto_fails_on_a_login_wall(httpserver, wc):
    # browser="auto" reads login_required and FAILS -- no transport fixes credentials.
    httpserver.expect_request("/wall").respond_with_data(
        "<h1>Sign in</h1><form><input type='password'></form>", content_type="text/html",
    )
    with pytest.raises(WebException) as info:
        wc.fetch(httpserver.url_for("/wall"), browser="auto")
    assert info.value.error is not None and info.value.error.type == "LoginRequired"
    # opt out (optional=True) -> the not-ok doc comes back, the flag still readable
    doc = wc.ref(httpserver.url_for("/wall")).resolve(browser="auto", optional=True)
    assert not doc.ok and doc.login_required().present


# -- resolve policy declared to a proxy service as request headers -------------


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


# -- the browser="auto" adaptive ladder (needs a real browser) -----------------

_SPA = (
    "<html><body><div id='root'></div><script>"
    "document.getElementById('root').innerHTML="
    "'<h1>Loaded content here</h1>' + 'x'.repeat(300);"
    "</script></body></html>"
)


def test_browser_auto_escalates_a_js_gated_page(httpserver, wc):
    httpserver.expect_request("/spa").respond_with_data(_SPA, content_type="text/html")
    url = httpserver.url_for("/spa")
    # a plain static fetch: an empty SPA shell -> spa flag fires with a browser remedy,
    # and it stayed on the static tier (no escalation requested)
    static = wc.fetch(url)
    assert static.spa().present and static.spa().remedy == "browser"
    assert static.transport().final_tier == "static"
    # browser="auto": the JS-gated page is escalated to a browser render
    auto = wc.fetch(url, browser="auto")
    assert "Loaded content" in (auto.text_content or "")  # JS ran
    assert auto.transport().final_tier == "browser"
    assert auto.transport().escalation == ["static", "browser"]


def test_browser_auto_stays_static_for_a_normal_page(httpserver, wc):
    httpserver.expect_request("/plain").respond_with_data(
        "<html><body><h1>Fine</h1>" + "real content " * 80 + "</body></html>",
        content_type="text/html",
    )
    doc = wc.fetch(httpserver.url_for("/plain"), browser="auto")
    assert doc._page is None  # never launched a browser
    assert doc.transport().final_tier == "static" and doc.flags() == []
