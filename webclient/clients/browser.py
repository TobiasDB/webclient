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

    async def open(
        self,
        url: str,
        *,
        scripts: "tuple[PageScript, ...] | list[PageScript]" = (),
        replay: "list[dict[str, Any]]" = [],
    ) -> PageResult:
        """Navigate to ``url``, installing ``scripts`` by phase (``init`` before
        nav, ``load`` once after, ``drain`` after any replay), capturing console +
        network requests, and replaying any recorded actions. Returns the raw page
        facts; the domain (document + events) is built by the caller."""
        page = self.page
        for s in scripts:  # init scripts must be installed before navigation
            if s.phase == "init":
                await page.add_init_script(s.source)
        console: list[tuple[str, str]] = []
        page.on("console", lambda m: console.append((m.type, m.text)))
        network: list[tuple[str, str, str]] = []
        page.on("request", lambda r: network.append((r.method, r.url, r.resource_type)))
        await page.goto(url)
        # snapshot the load-time console/network (replay-time noise is discarded,
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
