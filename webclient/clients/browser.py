"""The browser transport client -- one playwright page and the browser it comes
from."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .base import Client, ClientFactory


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
        replay: "list[dict[str, Any]]" = [],
        drain_js: str | None = None,
    ) -> PageResult:
        """Navigate to ``url``, capturing console + network requests, replaying any
        recorded actions, and (optionally) evaluating ``drain_js`` to discard load
        mutations. Returns the raw page facts; the domain (document + events) is
        built by the caller."""
        page = self.page
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
        for step in replay:  # reproduce recorded interactions (click / write)
            args = step.get("args", {})
            loc = page.locator(args.get("selector") or "*").first
            if step["op"] == "click":
                await loc.click()
            elif step["op"] == "write":
                await loc.fill(args.get("text", "") or "")
        if drain_js:
            await page.evaluate(drain_js)
        return result

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
