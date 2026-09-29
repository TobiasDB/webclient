"""web.fetch.chrome -- the browser SUPPLY (launch vs attach) and binary resolution, unit-tested
with a fake Playwright so no real browser is launched."""

from __future__ import annotations

import asyncio
import os
import sys

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
        self.driver = "playwright"
        self.needs_display = False
        self._key = key
        self.browser = _FakeBrowser()

    async def connect(self, pw):
        return self.browser

    def key(self) -> object:
        return self._key


def _seeded_manager() -> "BrowserManager":
    """A manager with its runtime pre-seeded (fake), so acquire skips starting a real Playwright."""
    m = BrowserManager()
    m._runtimes = {"playwright": _FakePlaywright()}  # type: ignore[attr-defined]
    return m


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


def test_stealth_profile_selects_the_leak_patched_driver() -> None:
    from web.fetch import Profile

    stealthy = Profile(browser=True, stealth=True).fetcher()
    plain = Profile(browser=True).fetcher()
    assert stealthy._supply.driver == "patchright"  # type: ignore[union-attr]
    assert plain._supply.driver == "playwright"  # type: ignore[union-attr]
    # the driver is part of the launch identity, so stealthy and plain don't share a process
    assert LaunchSupply(driver="patchright").key() != LaunchSupply().key()


def test_manager_pools_a_process_by_identity_and_ref_counts_it() -> None:
    # two acquires of the same launch identity SHARE one process, closed only when the last releases.
    m = _seeded_manager()
    sup = _FakeSupply(owns=True)
    b1 = _run(m.acquire(sup))
    b2 = _run(m.acquire(sup))
    assert b1 is b2  # same identity -> one shared process
    _run(m.release(sup))
    assert b1.closed is False  # still one holder
    _run(m.release(sup))
    assert b1.closed is True  # last holder released -> a LAUNCHED process is closed


def test_manager_never_closes_an_attached_process() -> None:
    m = _seeded_manager()
    sup = _FakeSupply(owns=False)  # attached over CDP
    b = _run(m.acquire(sup))
    _run(m.release(sup))
    assert b.closed is False  # attached -> the remote/user process is left running


def test_manager_aclose_stops_the_runtime_and_closes_held_processes() -> None:
    m = _seeded_manager()
    pw = m._runtimes["playwright"]  # type: ignore[attr-defined]
    sup = _FakeSupply(owns=True)
    b = _run(m.acquire(sup))
    _run(m.aclose())
    assert b.closed is True and pw.stopped is True and not m._runtimes  # type: ignore[attr-defined]


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


def test_headed_needs_a_display_only_on_a_displayless_linux_host() -> None:
    from web.fetch.browser import VirtualDisplay, display_needed

    assert LaunchSupply(headless=True).needs_display is False  # headless never needs one
    if os.environ.get("DISPLAY") or sys.platform != "linux":
        # a real display (this WSLg host) or non-Linux -> no virtual display, headed uses it as-is
        assert display_needed() is False
        assert LaunchSupply(headless=False).needs_display is False
        assert VirtualDisplay().start() is None  # no-op when a display already exists


def test_real_channel_resolves_the_genuine_binary(monkeypatch: "object") -> None:
    import web.fetch.browser.chrome as chrome

    monkeypatch.setattr(chrome, "real_chrome_path", lambda: "/usr/bin/google-chrome")  # type: ignore[attr-defined]
    assert LaunchSupply(channel="chrome")._executable() == "/usr/bin/google-chrome"
    # a bundled Chromium under stock Playwright resolves no explicit binary (channel handles it)
    assert LaunchSupply(channel="chromium", driver="playwright")._executable() is None
