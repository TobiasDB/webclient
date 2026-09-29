"""web.fetch.chrome -- the browser SUPPLY (launch vs attach) and binary resolution, unit-tested
with a fake Playwright so no real browser is launched."""

from __future__ import annotations

import asyncio

from web.fetch import (
    BrowserFetcher,
    BrowserManager,
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
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class _FakeSupply:
    """A supply that hands the manager a fresh fake browser; `owns_process` drives close-on-release."""

    def __init__(self, owns: bool, key: str = "k") -> None:
        self.owns_process = owns
        self._key = key
        self.browser = _FakeBrowser()

    async def connect(self, pw):
        return self.browser

    def key(self) -> object:
        return self._key


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


def test_manager_pools_a_process_by_identity_and_ref_counts_it() -> None:
    # two acquires of the same launch identity SHARE one process, closed only when the last releases.
    m = BrowserManager()
    m._pw = (
        _FakePlaywright()
    )  # pre-set so acquire skips starting a real Playwright  # type: ignore[assignment]
    sup = _FakeSupply(owns=True)
    b1 = _run(m.acquire(sup))
    b2 = _run(m.acquire(sup))
    assert b1 is b2  # same identity -> one shared process
    _run(m.release(sup))
    assert b1.closed is False  # still one holder
    _run(m.release(sup))
    assert b1.closed is True  # last holder released -> a LAUNCHED process is closed


def test_manager_never_closes_an_attached_process() -> None:
    m = BrowserManager()
    m._pw = _FakePlaywright()  # type: ignore[assignment]
    sup = _FakeSupply(owns=False)  # attached over CDP
    b = _run(m.acquire(sup))
    _run(m.release(sup))
    assert b.closed is False  # attached -> the remote/user process is left running


def test_manager_aclose_stops_playwright_and_closes_held_processes() -> None:
    m = BrowserManager()
    pw = _FakePlaywright()
    m._pw = pw  # type: ignore[assignment]
    sup = _FakeSupply(owns=True)
    b = _run(m.acquire(sup))
    _run(m.aclose())
    assert b.closed is True and pw.stopped is True and m._pw is None


def test_pool_shares_one_manager_across_browser_tiers() -> None:
    from web.fetch import ClientPool, Profile

    pool = ClientPool()
    a = pool.lease(Profile(browser=True))
    b = pool.lease(Profile(browser=True, headless=False))
    # different browser profiles, but they were handed the pool's ONE shared manager
    assert a._manager is b._manager is pool._browser_manager  # type: ignore[attr-defined]


def test_real_chrome_path_returns_a_path_or_none() -> None:
    # resolves a genuine Chrome (not the bundled Chrome-for-Testing); None when none is installed.
    p = real_chrome_path()
    assert p is None or isinstance(p, str)
