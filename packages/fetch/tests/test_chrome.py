"""web.fetch.chrome -- the browser SUPPLY (launch vs attach) and binary resolution, unit-tested
with a fake Playwright so no real browser is launched."""

from __future__ import annotations

import asyncio

from web.fetch import (
    BrowserFetcher,
    CdpSupply,
    LaunchSupply,
    real_chrome_path,
    supply_for,
)


def _run(coro):
    return asyncio.run(coro)


class _FakeBrowser:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self) -> None:
        self.launched: "dict | None" = None
        self.connected: "str | None" = None

    async def launch(self, **kw):
        self.launched = kw
        return _FakeBrowser()

    async def connect_over_cdp(self, endpoint):
        self.connected = endpoint
        return _FakeBrowser()


class _FakePlaywright:
    def __init__(self) -> None:
        self.chromium = _FakeChromium()

    async def stop(self) -> None:
        pass


def test_launch_supply_launches_a_real_browser_not_a_harness() -> None:
    pw = _FakePlaywright()
    sup = LaunchSupply(headless=False, channel="chrome")
    _run(sup.connect(pw))
    kw = pw.chromium.launched
    assert kw is not None and kw["headless"] is False and kw["channel"] == "chrome"
    # the anti-automation launch args are always present (ANTI-BOT.md §2.3)
    assert "--disable-blink-features=AutomationControlled" in kw["args"]
    assert "--enable-automation" in kw["ignore_default_args"]
    assert sup.owns_process is True  # we launched it -> we close it


def test_launch_supply_maps_bundled_chromium_to_no_channel() -> None:
    pw = _FakePlaywright()
    _run(LaunchSupply(channel="chromium").connect(pw))
    assert pw.chromium.launched is not None and pw.chromium.launched["channel"] is None


def test_cdp_supply_attaches_and_does_not_own_the_process() -> None:
    pw = _FakePlaywright()
    sup = CdpSupply(endpoint="http://host:9222")
    _run(sup.connect(pw))
    assert pw.chromium.connected == "http://host:9222"
    assert sup.owns_process is False  # attached -> never kill the remote/user process


def test_supply_for_picks_cdp_or_launch() -> None:
    assert isinstance(supply_for(cdp="http://x:9222"), CdpSupply)
    assert isinstance(supply_for(channel="chrome"), LaunchSupply)


def test_browser_fetcher_derives_its_supply_from_kwargs() -> None:
    assert isinstance(BrowserFetcher(cdp="http://x:9222")._supply, CdpSupply)
    assert isinstance(BrowserFetcher(channel="chrome")._supply, LaunchSupply)


def test_attached_browser_is_left_running_on_close() -> None:
    # a CDP-attached fetcher must NOT close the remote browser -- only disconnect (stop Playwright).
    f = BrowserFetcher(cdp="http://x:9222")
    fake = _FakeBrowser()
    f._browser = fake  # type: ignore[assignment]
    f._pw = _FakePlaywright()  # type: ignore[assignment]
    _run(f.aclose())
    assert fake.closed is False


def test_launched_browser_is_closed_on_close() -> None:
    f = BrowserFetcher(channel="chromium")
    fake = _FakeBrowser()
    f._browser = fake  # type: ignore[assignment]
    f._pw = _FakePlaywright()  # type: ignore[assignment]
    _run(f.aclose())
    assert fake.closed is True


def test_real_chrome_path_returns_a_path_or_none() -> None:
    # resolves a genuine Chrome (not the bundled Chrome-for-Testing); None when none is installed.
    p = real_chrome_path()
    assert p is None or isinstance(p, str)
