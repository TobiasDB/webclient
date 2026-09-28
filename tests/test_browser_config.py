"""Browser configuration (stealth / headless / fingerprint) + the HTTP/2 anti-bot
escalation. These exercise the config plumbing and the ladder branch without
launching a real browser."""

from webclient import BrowserConfig, WebClient
from webclient.clients.browser import BrowserFactory, _random_fingerprint
from webclient.core.client import _looks_like_bot_block
from webclient.core.document import Document
from webclient.kernel.errors import error_for


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
    from webclient.policy import BrowserConfig

    with WebClient(browser_config=BrowserConfig(proxy="http://user:pw@127.0.0.1:8888")) as wc:
        factories = wc.pool._factories
        assert factories["http"].proxy == "http://user:pw@127.0.0.1:8888"
        assert factories["page"].proxy == "http://user:pw@127.0.0.1:8888"
    # default = no proxy (a direct connection)
    with WebClient() as wc2:
        assert wc2.pool._factories["http"].proxy is None
        assert wc2.pool._factories["page"].proxy is None


# -- CDP / remote / reuse-context (Phase 1): connect instead of launch ---------

def test_cdp_config_threads_through_to_the_factory():
    cfg = BrowserConfig(cdp_endpoint="http://127.0.0.1:9222", reuse_context=True)
    assert cfg.cdp_endpoint == "http://127.0.0.1:9222" and cfg.reuse_context is True
    assert cfg.ws_endpoint is None
    with WebClient(browser_config=cfg) as wc:
        page = wc.pool._factories["page"]
        assert page.cdp_endpoint == "http://127.0.0.1:9222" and page.reuse_context is True


def test_reuse_context_auto_is_on_for_cdp_and_off_otherwise():
    # None (auto) -> reuse for a cdp_endpoint ("my browser"), fresh context otherwise.
    assert BrowserFactory(cdp_endpoint="http://h:9222")._reuse_context is True
    assert BrowserFactory(ws_endpoint="ws://h/x")._reuse_context is False
    assert BrowserFactory()._reuse_context is False
    # explicit wins over the auto default either way.
    assert BrowserFactory(cdp_endpoint="http://h:9222", reuse_context=False)._reuse_context is False
    assert BrowserFactory(ws_endpoint="ws://h/x", reuse_context=True)._reuse_context is True


class _FakePage:
    async def close(self):
        pass


class _FakeContext:
    def __init__(self):
        self.pages_opened = 0
        self.closed = False
        self.init_scripts = 0

    async def add_init_script(self, _script):
        self.init_scripts += 1

    async def new_page(self):
        self.pages_opened += 1
        return _FakePage()

    async def close(self):
        self.closed = True


class _FakeBrowser:
    version = "141.0.7000.0"

    def __init__(self, *, existing_context=None):
        self.contexts = [existing_context] if existing_context is not None else []
        self.new_contexts = []
        self.closed = False

    async def new_context(self, **_opts):
        ctx = _FakeContext()
        self.new_contexts.append(ctx)
        return ctx

    async def close(self):
        self.closed = True


class _FakeChromium:
    """Records which connection method the factory chose."""

    def __init__(self, browser):
        self._browser = browser
        self.calls = []  # (method, arg)

    async def launch(self, **kw):
        self.calls.append(("launch", kw))
        return self._browser

    async def connect_over_cdp(self, endpoint):
        self.calls.append(("connect_over_cdp", endpoint))
        return self._browser

    async def connect(self, ws):
        self.calls.append(("connect", ws))
        return self._browser


class _FakePW:
    def __init__(self, chromium):
        self.chromium = chromium
        self.stopped = False

    async def stop(self):
        self.stopped = True


def _install_fake_playwright(monkeypatch, browser):
    chromium = _FakeChromium(browser)

    class _Starter:
        async def start(self):
            return _FakePW(chromium)

    import playwright.async_api as pa
    monkeypatch.setattr(pa, "async_playwright", lambda: _Starter())
    return chromium


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_cdp_endpoint_attaches_over_cdp_and_reuses_the_context(monkeypatch):
    existing = _FakeContext()  # the user's real, logged-in context
    browser = _FakeBrowser(existing_context=existing)
    chromium = _install_fake_playwright(monkeypatch, browser)

    async def main():
        f = BrowserFactory(cdp_endpoint="http://127.0.0.1:9222")  # reuse auto-on for cdp
        await f._browser_()
        assert f._connected is True
        assert chromium.calls == [("connect_over_cdp", "http://127.0.0.1:9222")]
        await f.create()
        # reused the existing context (opened a page in it); no fresh context, no stealth inject
        assert existing.pages_opened == 1 and browser.new_contexts == []
        assert existing.init_scripts == 0
        await f.aclose()
        # a connected browser is DETACHED, never killed; its context is the user's, left open
        assert browser.closed is False and existing.closed is False

    _run(main())


def test_ws_endpoint_attaches_to_a_server_with_a_fresh_context(monkeypatch):
    browser = _FakeBrowser()  # a remote pool: no pre-existing context
    chromium = _install_fake_playwright(monkeypatch, browser)

    async def main():
        f = BrowserFactory(ws_endpoint="ws://host/abc")  # reuse auto-off -> fresh context
        await f._browser_()
        assert f._connected is True
        assert chromium.calls == [("connect", "ws://host/abc")]
        await f.create()
        assert len(browser.new_contexts) == 1  # opened our own isolated context
        await f.aclose()
        assert browser.closed is False  # connected -> detach, don't kill
        assert browser.new_contexts[0].closed is True  # but close the context WE opened

    _run(main())


def test_default_launches_and_aclose_kills_the_browser(monkeypatch):
    browser = _FakeBrowser()
    chromium = _install_fake_playwright(monkeypatch, browser)

    async def main():
        f = BrowserFactory()  # no endpoints -> launch
        await f._browser_()
        assert f._connected is False
        assert chromium.calls[0][0] == "launch"
        await f.aclose()
        assert browser.closed is True  # we launched it, so we kill it

    _run(main())


def test_page_release_frees_its_context_but_not_a_reused_one(monkeypatch):
    # M2: releasing a page closes ITS context (no per-lease accumulation until teardown)...
    browser = _FakeBrowser()
    _install_fake_playwright(monkeypatch, browser)

    async def launched():
        f = BrowserFactory()  # launch -> fresh context per page, owned
        client = await f.create()
        ctx = browser.new_contexts[0]
        assert client._owns_context is True
        await client.aclose()
        assert ctx.closed is True  # freed on release

    _run(launched())

    # ...but a reused (connected "my browser") context is the user's -- never closed on release.
    existing = _FakeContext()
    browser2 = _FakeBrowser(existing_context=existing)
    _install_fake_playwright(monkeypatch, browser2)

    async def reused():
        f = BrowserFactory(cdp_endpoint="http://h:9222")  # reuse auto-on
        client = await f.create()
        assert client._owns_context is False
        await client.aclose()
        assert existing.closed is False  # user's session context left open

    _run(reused())
