"""The browser transport client -- one playwright page and the browser it comes
from."""

from __future__ import annotations

from typing import Any

from .base import Client, ClientFactory


class BrowserClient(Client):
    """One browser page (the leased unit for live documents)."""

    kind = "page"

    def __init__(self, page: Any) -> None:
        self.page = page

    async def aclose(self) -> None:
        await self.page.close()


class BrowserFactory(ClientFactory):
    """Owns one lazily-launched browser; each ``create`` opens a fresh page
    (with an optional init script installed before navigation)."""

    kind = "page"

    def __init__(
        self, *, headless: bool = True, init_script: str | None = None
    ) -> None:
        self.headless = headless
        self.init_script = init_script
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
        page = await browser.new_page()
        if self.init_script:
            await page.add_init_script(self.init_script)
        return BrowserClient(page)

    async def aclose(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            await self._pw.stop()
            self._browser = self._pw = None


__all__ = ["BrowserClient", "BrowserFactory"]
