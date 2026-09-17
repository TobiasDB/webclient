"""Browser configuration (stealth / headless / fingerprint) + the HTTP/2 anti-bot
escalation. These exercise the config plumbing and the ladder branch without
launching a real browser."""

from webclient import BrowserConfig, WebClient
from webclient.clients.browser import BrowserFactory, _random_fingerprint
from webclient.core.client import _looks_like_bot_block
from webclient.core.document import Document
from webclient.errors import error_for


def test_browser_config_defaults_to_stealth():
    cfg = BrowserConfig()
    assert cfg.stealth and cfg.headless and not cfg.fingerprint  # stealth by default
    auto = BrowserConfig.auto()
    assert auto.stealth and auto.fingerprint  # the hardened variant randomises identity


def test_browser_factory_carries_the_config():
    f = BrowserFactory(headless=False, stealth=True, fingerprint=True)
    assert f.headless is False and f.stealth and f.fingerprint


def test_random_fingerprint_is_from_the_pool():
    fp = _random_fingerprint()
    assert {"ua", "vw", "vh", "locale", "tz"} <= set(fp)
    assert "Mozilla/5.0" in fp["ua"]


def test_client_threads_browser_config_into_the_factory():
    with WebClient(browser_config=BrowserConfig(headless=False, fingerprint=True)) as wc:
        page = wc.pool._factories["page"]  # the leased-page factory
        assert page.headless is False and page.fingerprint and page.stealth


def test_looks_like_bot_block_matches_http2_and_resets():
    assert _looks_like_bot_block(error_for(0, "RemoteProtocolError: ERR_HTTP2_PROTOCOL_ERROR"))
    assert _looks_like_bot_block(error_for(0, "connection reset by peer"))
    assert not _looks_like_bot_block(error_for(404))  # an ordinary status is not a block
    assert not _looks_like_bot_block(None)


def test_http2_protocol_error_escalates_to_a_browser():
    # under auto, a protocol-level block is read as an anti-bot trigger and escalated to
    # a browser (whose real HTTP/2 stack often gets through) rather than surfaced.
    async def fake_once(ref, headers):
        doc = Document(
            url=ref.dispatch("url"), status_code=0,
            error=error_for(0, "ERR_HTTP2_PROTOCOL_ERROR"),
        )
        return doc, None

    sentinel = Document(url="https://x.co/", status_code=200, content=b"<p>ok</p>", kind="html")

    async def fake_browser(ref, *a, **k):
        return sentinel

    with WebClient() as wc:  # retries=0, so it escalates immediately
        wc._afetch_once = fake_once  # type: ignore[method-assign]
        wc._escalate_to_browser = fake_browser  # type: ignore[method-assign]
        doc = wc.fetch("https://x.co/", browser="auto", optional=True)
    assert doc is sentinel  # escalated, not the errored static doc


def test_stealth_fingerprints_are_internally_consistent():
    # each identity's UA / platform / WebGL / cores agree (an inconsistent fingerprint --
    # Windows UA + Linux platform + Mac GPU -- is itself a bot tell), and the injected
    # identity script carries those same values.
    from webclient.clients.browser import _DEFAULT_FP, _FINGERPRINTS, _identity_js

    for fp in _FINGERPRINTS:
        ua = fp["ua"]
        if "Windows" in ua:
            assert fp["platform"] == "Win32" and "NVIDIA" in fp["gpu_renderer"]
        elif "Macintosh" in ua:
            assert fp["platform"] == "MacIntel" and "Apple" in fp["gpu_renderer"]
        else:
            assert "Linux" in ua and fp["platform"] == "Linux x86_64" and "Intel" in fp["gpu_renderer"]
        js = _identity_js(fp)  # the injected script uses this identity's own values
        assert fp["platform"] in js and fp["gpu_vendor"] in js and str(fp["cores"]) in js
    assert _DEFAULT_FP["platform"] == "Linux x86_64"  # default identity is coherent with the host


def test_client_wide_proxy_reaches_http_and_browser_factories():
    # one BrowserConfig.proxy routes BOTH the httpx client and the browser through the same proxy.
    from webclient import WebClient
    from webclient.core.reference.models import BrowserConfig

    with WebClient(browser_config=BrowserConfig(proxy="http://user:pw@127.0.0.1:8888")) as wc:
        factories = wc.pool._factories
        assert factories["http"].proxy == "http://user:pw@127.0.0.1:8888"
        assert factories["page"].proxy == "http://user:pw@127.0.0.1:8888"
    # default = no proxy (a direct connection)
    with WebClient() as wc2:
        assert wc2.pool._factories["http"].proxy is None
        assert wc2.pool._factories["page"].proxy is None
