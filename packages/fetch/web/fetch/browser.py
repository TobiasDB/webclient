"""``BrowserFetcher`` and ``LivePage`` -- the browser transport (Playwright), behind the same
:class:`~web.fetch.base.Fetcher` interface as the static one.

Two ways to use it. As a plain Fetcher, :meth:`BrowserFetcher.fetch` renders a page and returns
its Snapshot -- the JS-executed DOM, which a static fetch cannot produce (this is what an
``spa`` escalation upgrades to). For interaction, :meth:`BrowserFetcher.open` returns a
:class:`LivePage`: its actions (``click`` / ``type`` / ``wait_for``) drive the one page and
return **Self**, so a chain of them costs nothing until :meth:`LivePage.snapshot` materialises
the result. Playwright is imported lazily, so importing web.fetch never requires it.
"""

from __future__ import annotations

import time
from typing import Any

from web.kernel import Event, emit

from .errors import classify
from .events import DOMEvent, FetchEvent, NetworkEvent
from .request import Request
from .script import DOM_RECORDER, Script
from .snapshot import Snapshot


class LivePage:
    """A live browser page. Actions return Self (drive without snapshotting); ``snapshot``
    captures the current DOM as a :class:`Snapshot`, draining each script's recorder into a
    DOMEvent and attaching the NetworkEvents seen so far."""

    def __init__(
        self, page: Any, request: Request, status: int,
        scripts: tuple[Script, ...], network: list[NetworkEvent],
    ) -> None:
        self._page = page
        self._request = request
        self._status = status
        self._scripts = scripts
        self._network = network

    async def click(self, selector: str) -> "LivePage":
        await self._page.click(selector)
        return self

    async def type(self, selector: str, text: str) -> "LivePage":
        await self._page.fill(selector, text)
        return self

    async def wait_for(self, selector: str) -> "LivePage":
        await self._page.wait_for_selector(selector)
        return self

    async def snapshot(self) -> Snapshot:
        content: str = await self._page.content()
        emit(FetchEvent(url=self._page.url, status=self._status, source="browser"))
        events: list[Event] = list(self._network)
        for s in self._scripts:  # drain each recorder into a DOMEvent
            if s.drain:
                records = await self._page.evaluate(s.drain)
                if records:
                    events.append(DOMEvent(script=s.name, records=records))
        return Snapshot(
            request=self._request,
            url=self._page.url,
            status=self._status,
            headers={"content-type": "text/html; charset=utf-8"},
            content=content.encode("utf-8"),
            events=events,
        )

    async def close(self) -> None:
        await self._page.close()


class BrowserFetcher:
    """A browser :class:`~web.fetch.base.Fetcher` over Playwright/Chromium (lazily launched)."""

    def __init__(
        self, *, headless: bool = True, channel: str = "chromium",
        proxy: str | None = None, fingerprint: bool = False,
        scripts: tuple[Script, ...] = (DOM_RECORDER,),
    ) -> None:
        self._headless = headless
        self._channel = channel  # "chromium" = bundled; "chrome" = the real Chrome install
        self._proxy = proxy
        self._fingerprint = fingerprint
        self._scripts = scripts
        self._pw: Any = None
        self._browser: Any = None

    async def _browser_ready(self) -> Any:
        if self._browser is None:
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=self._headless,
                channel=None if self._channel == "chromium" else self._channel,
                proxy={"server": self._proxy} if self._proxy else None,
            )
        return self._browser

    async def open(self, request: Request) -> LivePage:
        """Navigate to the request and return a :class:`LivePage` to drive. Installs each page
        script and captures every response as a NetworkEvent along the way."""
        browser = await self._browser_ready()
        page = await browser.new_page()
        if self._fingerprint:  # a light stealth pass (real anti-detect is a heavier backend)
            await page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        network: list[NetworkEvent] = []
        page.on("response", lambda r: network.append(NetworkEvent(
            method=r.request.method, url=r.url, status=r.status, resource_type=r.request.resource_type)))
        try:
            for s in self._scripts:  # 'init' scripts run before any page script
                if s.on == "init":
                    await page.add_init_script(s.js)
            resp = await page.goto(request.url, wait_until="load", timeout=request.timeout * 1000)
            for s in self._scripts:  # 'load' recorders installed after the initial render
                if s.on == "load":
                    await page.evaluate(s.js)
        except BaseException:  # a nav failure must not leak the page we opened
            await page.close()
            raise
        return LivePage(page, request, resp.status if resp is not None else 0, self._scripts, network)

    async def fetch(self, request: Request) -> Snapshot:
        """Render the request to a Snapshot. Never raises for a fetch failure (a nav timeout /
        error becomes ``snapshot.error``); the page is always closed."""
        start = time.perf_counter()
        try:
            page = await self.open(request)
        except Exception as exc:  # open() already cleaned up its page
            return Snapshot(request=request, url=request.url, elapsed=time.perf_counter() - start,
                            error=classify(exc, url=request.url))
        try:
            return await page.snapshot()
        except Exception as exc:
            return Snapshot(request=request, url=request.url, elapsed=time.perf_counter() - start,
                            error=classify(exc, url=request.url))
        finally:
            await page.close()

    async def aclose(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None  # idempotent: a Resolver closes every tier, callers may too
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None


__all__ = ["BrowserFetcher", "LivePage"]
