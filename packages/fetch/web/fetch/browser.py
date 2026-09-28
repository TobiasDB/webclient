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

from typing import Any

from .request import Request
from .snapshot import Snapshot


class LivePage:
    """A live browser page. Actions return Self (drive without snapshotting); ``snapshot``
    captures the current DOM as a :class:`Snapshot`."""

    def __init__(self, page: Any, request: Request, status: int) -> None:
        self._page = page
        self._request = request
        self._status = status

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
        return Snapshot(
            request=self._request,
            url=self._page.url,
            status=self._status,
            headers={"content-type": "text/html; charset=utf-8"},
            content=content.encode("utf-8"),
        )

    async def close(self) -> None:
        await self._page.close()


class BrowserFetcher:
    """A browser :class:`~web.fetch.base.Fetcher` over Playwright/Chromium (lazily launched)."""

    def __init__(self, *, headless: bool = True) -> None:
        self._headless = headless
        self._pw: Any = None
        self._browser: Any = None

    async def _browser_ready(self) -> Any:
        if self._browser is None:
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=self._headless)
        return self._browser

    async def open(self, request: Request) -> LivePage:
        """Navigate to the request and return a :class:`LivePage` to drive."""
        browser = await self._browser_ready()
        page = await browser.new_page()
        resp = await page.goto(request.url, wait_until="load", timeout=request.timeout * 1000)
        return LivePage(page, request, resp.status if resp is not None else 0)

    async def fetch(self, request: Request) -> Snapshot:
        page = await self.open(request)
        try:
            return await page.snapshot()
        finally:
            await page.close()

    async def aclose(self) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()


__all__ = ["BrowserFetcher", "LivePage"]
