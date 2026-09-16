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
