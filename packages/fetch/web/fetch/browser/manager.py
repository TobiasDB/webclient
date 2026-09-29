"""``BrowserManager`` -- the browser-PROCESS management layer.

One place owns the Playwright runtime and the live browser processes, so the rest of web.fetch
never starts or stops one directly. A :class:`~web.fetch.browser.backend.BrowserFetcher` asks the
manager for a browser and hands it back; the manager keeps ONE Playwright and POOLS processes by
their launch identity (:meth:`BrowserSupply.key`), ref-counted, closing a process only when the
last fetcher releases it -- and only when the supply LAUNCHED it (an attached remote/user Chrome is
never killed).

Why it matters here: the realness ladder (ANTI-BOT.md §5) is several browser tiers, and a pool
leases a fetcher per tier. Without a manager each tier would spin up its own Playwright + process;
with one, they share a single runtime and identical supplies share a process. This is req #1 of the
browser abstraction -- centralised process lifecycle -- kept inside fetch, with no notion of flags.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from .chrome import BrowserSupply

if TYPE_CHECKING:
    from playwright.async_api import Browser, Playwright


async def _start_runtime(driver: str) -> "Playwright":
    """Start the Playwright runtime for ``driver``: the leak-patched ``patchright`` fork when asked
    for (and installed) -- it suppresses the CDP ``Runtime.enable`` leak (ANTI-BOT.md §5) -- else
    stock Playwright. patchright is a DROP-IN fork with an identical async API (only its nominal
    types differ), so the supply/session code is driver-agnostic; we bridge the type at this one
    seam. An uninstalled patched driver falls back to Playwright (unpatched, but working)."""
    if driver == "patchright":
        try:
            import patchright.async_api as _patchright  # the leak-patched fork (optional extra)

            return cast("Playwright", await _patchright.async_playwright().start())
        except ImportError:
            pass
    from playwright.async_api import async_playwright

    return await async_playwright().start()


@dataclass
class _Lease:
    """One pooled browser process: the live ``Browser``, how many fetchers hold it, and the supply
    that produced it (so release knows whether it OWNS the process and may close it)."""

    browser: "Browser"
    refs: int
    supply: BrowserSupply


class BrowserManager:
    """Owns the single Playwright runtime and a ref-counted pool of browser processes, shared across
    every :class:`BrowserFetcher` that uses this manager. Thread-safe over one event loop via an
    internal lock, so concurrent acquires of the same identity share one process."""

    def __init__(self) -> None:
        #: one Playwright runtime PER DRIVER (stock vs leak-patched), started lazily and shared.
        self._runtimes: "dict[str, Playwright]" = {}
        self._leases: "dict[object, _Lease]" = {}
        self._lock = asyncio.Lock()

    async def acquire(self, supply: BrowserSupply) -> "Browser":
        """The browser for ``supply`` -- launched/attached on first use, reused (ref-count bumped)
        thereafter. Starts the runtime for the supply's driver lazily on first use."""
        async with self._lock:
            runtime = self._runtimes.get(supply.driver)
            if runtime is None:
                runtime = await _start_runtime(supply.driver)
                self._runtimes[supply.driver] = runtime
            key = supply.key()
            lease = self._leases.get(key)
            if lease is None:
                browser = await supply.connect(runtime)
                self._leases[key] = _Lease(browser=browser, refs=1, supply=supply)
                return browser
            lease.refs += 1
            return lease.browser

    async def release(self, supply: BrowserSupply) -> None:
        """Drop one hold on ``supply``'s process; when the last is released, close it -- but only if
        the supply LAUNCHED it (an attached CDP process is left running). The Playwright runtime
        stays up for other/future leases; :meth:`aclose` shuts it."""
        async with self._lock:
            key = supply.key()
            lease = self._leases.get(key)
            if lease is None:
                return
            lease.refs -= 1
            if lease.refs <= 0:
                del self._leases[key]
                if lease.supply.owns_process:
                    try:
                        await lease.browser.close()
                    except Exception:  # a browser already gone must not break teardown
                        pass

    async def aclose(self) -> None:
        """Shut the whole layer: close every launched process this manager still holds (leaving any
        attached one running) and stop every driver runtime. Idempotent."""
        async with self._lock:
            for lease in self._leases.values():
                if lease.supply.owns_process:
                    try:
                        await lease.browser.close()
                    except Exception:
                        pass
            self._leases.clear()
            for runtime in self._runtimes.values():
                await runtime.stop()
            self._runtimes.clear()


__all__ = ["BrowserManager"]
