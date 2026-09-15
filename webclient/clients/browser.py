"""The browser transport client -- one playwright page and the browser it comes
from -- plus ``PageScript``, the unit of script injection.

A ``PageScript`` is content the client installs on a page at a named phase:
``"init"`` (via ``add_init_script`` -- runs before every navigation, e.g. a
mutation observer) or ``"load"`` (evaluated once after navigation). The *what*
comes from above (a backing declares its scripts, the core gathers them); the
client just installs them. So the injection point is transport, the scripts are
domain -- clean layering.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .base import Client, ClientFactory

Phase = Literal["init", "load", "drain"]


@dataclass(frozen=True)
class PageScript:
    """Script to install on a browser page. ``init`` runs before each navigation
    (``add_init_script``); ``load`` is evaluated once after navigation; ``drain``
    is evaluated after any replay (to clear a buffer, e.g. discard load-time DOM
    mutations) and its result is ignored."""

    source: str
    phase: Phase = "init"


@dataclass
class PageResult:
    """The raw result of driving a page -- the transport-level facts; the caller
    builds the document and wraps the console/network tuples into events."""

    final_url: str
    content: bytes
    console: list[tuple[str, str]] = field(default_factory=list)  # (level, text)
    network: list[tuple[str, str, str]] = field(  # (method, url, resource_type)
        default_factory=list
    )


class BrowserClient(Client):
    """One browser page (the leased unit for live documents) -- it owns the actual
    browser driving; the live document ops (``LiveBacking``) then interact with
    ``self.page`` directly."""

    kind = "page"

    def __init__(self, page: Any) -> None:
        self.page = page

    async def _wait_stable(
        self, page: Any, *, timeout: float = 8.0, quiet: float = 0.4, poll: float = 0.2
    ) -> None:
        """Wait for the DOM to settle so JS/lazy-loaded content is present before the
        snapshot: first let the network go idle (bounded -- many sites never truly
        idle), then poll the element count until it is unchanged for ``quiet`` seconds.
        Returns early the moment it's stable, so a page that settles quickly costs
        little; a JS page waits just until it stops mutating. ``timeout`` bounds the
        *whole* wait (the network-idle phase counts against it), so a page that never
        settles can never block longer than ``timeout``."""
        import time as _time

        deadline = _time.monotonic() + timeout  # set first: the total budget
        idle_ms = min(timeout, 3.0) * 1000
        try:  # a bounded network-idle wait; ignore if it never idles
            await page.wait_for_load_state("networkidle", timeout=idle_ms)
        except Exception:
            pass
        need = max(1, round(quiet / poll))
        last, stable = -1, 0
        while _time.monotonic() < deadline:
            try:
                count = await page.evaluate("() => document.getElementsByTagName('*').length")
            except Exception:
                return
            if count == last:
                stable += 1
                if stable >= need:
                    return
            else:
                last, stable = count, 0
            await page.wait_for_timeout(poll * 1000)

    async def open(
        self,
        url: str,
        *,
        scripts: "tuple[PageScript, ...] | list[PageScript]" = (),
        replay: "list[dict[str, Any]]" = [],
        wait_stable: bool = True,
    ) -> PageResult:
        """Navigate to ``url``, installing ``scripts`` by phase (``init`` before
        nav, ``load`` once after, ``drain`` after any replay), capturing console +
        network requests, and replaying any recorded actions. Returns the raw page
        facts; the domain (document + events) is built by the caller.

        ``wait_stable`` (default) waits for the DOM to settle after navigation, so
        JS/lazy-loaded content (links, cards, …) is in the snapshot -- the fix for
        a render that captured the shell before the page finished loading."""
        page = self.page
        for s in scripts:  # init scripts must be installed before navigation
            if s.phase == "init":
                await page.add_init_script(s.source)
        console: list[tuple[str, str]] = []
        page.on("console", lambda m: console.append((m.type, m.text)))
        network: list[tuple[str, str, str]] = []
        page.on("request", lambda r: network.append((r.method, r.url, r.resource_type)))
        await page.goto(url, wait_until="domcontentloaded")
        if wait_stable:  # let JS/lazy content load before snapshotting
            await self._wait_stable(page)
        # snapshot the settled console/network (replay-time noise is discarded,
        # like the drained DOM mutations)
        result = PageResult(
            page.url, (await page.content()).encode(), list(console), list(network)
        )
        for s in scripts:  # load scripts run once, after navigation
            if s.phase == "load":
                await page.evaluate(s.source)
        for step in replay:  # reproduce recorded interactions (click / write)
            args = step.get("args", {})
            loc = page.locator(args.get("selector") or "*").first
            if step["op"] == "click":
                await loc.click()
            elif step["op"] == "write":
                await loc.fill(args.get("text", "") or "")
        for s in scripts:  # drain scripts clear buffers after replay (result ignored)
            if s.phase == "drain":
                await page.evaluate(s.source)
        return result

    async def aclose(self) -> None:
        await self.page.close()


class BrowserFactory(ClientFactory):
    """Owns one lazily-launched browser; each ``create`` opens a fresh (blank)
    page. Script injection is per-navigation (``BrowserClient.open``), not baked
    into the factory, so the scripts can come from the core's backings."""

    kind = "page"

    def __init__(self, *, headless: bool = True) -> None:
        self.headless = headless
        self._pw: Any = None
        self._browser: Any = None

    async def _browser_(self) -> Any:
        if self._browser is None:
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=self.headless)
        return self._browser

    async def create(self) -> BrowserClient:
        browser = await self._browser_()
        return BrowserClient(await browser.new_page())

    async def aclose(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            await self._pw.stop()
            self._browser = self._pw = None


__all__ = ["BrowserClient", "BrowserFactory", "PageScript", "PageResult", "Phase"]
