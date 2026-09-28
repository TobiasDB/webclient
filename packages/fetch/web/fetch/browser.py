"""The browser backend (Playwright) and its :class:`BrowserSession`.

The browser backend implements the ``fetch(request) -> Snapshot`` protocol like any other, and
adds a stateful :class:`BrowserSession` for interaction. **The session OWNS its Playwright
context + page** and their whole lifecycle -- navigate with ``goto`` / ``fetch``, drive with
``click`` / ``type`` / ``wait_for`` (each returns Self, so a chain snapshots nothing), capture
with ``snapshot``, and ``aclose`` to release the page + context. Nothing outside the session
opens or closes its page; that ownership is the point.

``BrowserFetcher.fetch(request)`` is a one-shot: open a session, fetch, close. Playwright is
imported lazily, so importing web.fetch never requires it.
"""

from __future__ import annotations

import time
from typing import Any

from web.kernel import Event, emit

from .errors import classify
from .events import DOMEvent, FetchEvent, NetworkEvent
from .proxy import Proxy, as_proxy
from .request import Request
from .script import Script, ScriptRegistry, default_scripts
from .snapshot import Snapshot

_STEALTH = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"


class BrowserSession:
    """A browser session: it OWNS a Playwright context + page. Actions mutate the page and return
    Self; ``snapshot`` materialises a Snapshot; ``aclose`` closes the context (and its page)."""

    def __init__(self, context: Any, page: Any, scripts: tuple[Script, ...]) -> None:
        self._context = context
        self._page = page
        self._scripts = scripts
        self._request = Request(url=page.url or "about:blank")
        self._status = 0
        self._network: list[NetworkEvent] = []
        page.on("response", lambda r: self._network.append(NetworkEvent(
            method=r.request.method, url=r.url, status=r.status, resource_type=r.request.resource_type)))

    async def goto(self, request: Request) -> "BrowserSession":
        """Navigate the owned page to ``request`` (no snapshot); returns Self so navigation and
        actions chain. Installs the ``load`` recorders after the initial render."""
        self._request = request
        resp = await self._page.goto(request.url, wait_until="load", timeout=request.timeout * 1000)
        self._status = resp.status if resp is not None else 0
        for s in self._scripts:
            if s.on == "load":
                await self._page.evaluate(s.js)
        return self

    async def click(self, selector: str) -> "BrowserSession":
        await self._page.click(selector)
        return self

    async def type(self, selector: str, text: str) -> "BrowserSession":
        await self._page.fill(selector, text)
        return self

    async def wait_for(self, selector: str) -> "BrowserSession":
        await self._page.wait_for_selector(selector)
        return self

    async def snapshot(self) -> Snapshot:
        """Capture the current DOM as a Snapshot, draining each recorder into a DOMEvent and
        attaching the NetworkEvents seen so far."""
        content: str = await self._page.content()
        emit(FetchEvent(url=self._page.url, status=self._status, source="browser"))
        events: list[Event] = list(self._network)
        for s in self._scripts:
            if s.drain:
                records = await self._page.evaluate(s.drain)
                if records:
                    events.append(DOMEvent(script=s.name, records=records))
        return Snapshot(request=self._request, url=self._page.url, status=self._status,
                        headers={"content-type": "text/html; charset=utf-8"},
                        content=content.encode("utf-8"), events=events)

    async def fetch(self, request: Request) -> Snapshot:
        """Navigate + snapshot -- the Fetcher protocol within the session. Never raises: a nav
        failure becomes ``snapshot.error`` (classified)."""
        start = time.perf_counter()
        try:
            await self.goto(request)
        except Exception as exc:
            return Snapshot(request=request, url=request.url, elapsed=time.perf_counter() - start,
                            error=classify(exc, url=request.url))
        return await self.snapshot()

    async def aclose(self) -> None:
        """Close the context (and its page) -- the session owns them, so this is where they die."""
        await self._context.close()


class BrowserFetcher:
    """A browser backend over Playwright/Chromium (lazily launched). ``session()`` opens an owned
    :class:`BrowserSession`; ``fetch`` is a one-shot session."""

    def __init__(
        self, *, headless: bool = True, channel: str = "chromium",
        proxy: "str | Proxy | None" = None, fingerprint: bool = False,
        scripts: "tuple[Script, ...] | ScriptRegistry | None" = None,
    ) -> None:
        self._headless = headless
        self._channel = channel  # "chromium" = bundled; "chrome" = the real Chrome install
        self._proxy = as_proxy(proxy)
        self._fingerprint = fingerprint
        #: a registry so a caller can enable/disable capture scripts; a bare tuple is wrapped.
        self.scripts: ScriptRegistry = (
            default_scripts() if scripts is None
            else scripts if isinstance(scripts, ScriptRegistry)
            else ScriptRegistry(scripts)
        )
        self._pw: Any = None
        self._browser: Any = None

    async def _browser_ready(self) -> Any:
        if self._browser is None:
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=self._headless,
                channel=None if self._channel == "chromium" else self._channel,
                proxy=self._proxy.playwright() if self._proxy else None,
            )
        return self._browser

    async def session(self) -> BrowserSession:
        """Open a session that OWNS a fresh context + page (isolated cookies/state). Installs the
        stealth pass and any ``init`` scripts before navigation."""
        browser = await self._browser_ready()
        context = await browser.new_context()
        page = await context.new_page()
        scripts = self.scripts.enabled()  # only the enabled scripts install
        try:
            if self._fingerprint:  # a light stealth pass (real anti-detect is a heavier backend)
                await page.add_init_script(_STEALTH)
            for s in scripts:  # 'init' scripts run before any page script
                if s.on == "init":
                    await page.add_init_script(s.js)
        except BaseException:  # setup failed -> don't leak the context we opened
            await context.close()
            raise
        return BrowserSession(context, page, scripts)

    async def fetch(self, request: Request) -> Snapshot:
        """One-shot: open a session, fetch, close. Never raises (a session failure is
        ``snapshot.error``); the session -- and its page -- is always closed."""
        start = time.perf_counter()
        try:
            s = await self.session()
        except Exception as exc:
            return Snapshot(request=request, url=request.url, elapsed=time.perf_counter() - start,
                            error=classify(exc, url=request.url))
        try:
            return await s.fetch(request)
        finally:
            await s.aclose()

    async def aclose(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None  # idempotent
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None


__all__ = ["BrowserFetcher", "BrowserSession"]
