"""Leasable transport clients and their factories -- the units a ``ClientPool``
hands out.

    ClientPool.lease(kind) -> ClientFactory.create() -> Client

A ``Client`` is one unit of transport concurrency (one httpx client, or one
browser page). Factories know how to build (and tear down) each kind; the pool
bounds and recycles them. Nothing above the pool touches httpx or playwright
directly.
"""

from __future__ import annotations

import abc
from typing import Any

import httpx


class Client(abc.ABC):
    """A leased transport resource."""

    kind: str

    async def reset(self) -> None:
        """Clean per-lease state before the client is reused (default: none)."""

    @abc.abstractmethod
    async def aclose(self) -> None: ...


class HTTPXClient(Client):
    """One ``httpx.AsyncClient``. Cookies are passed per request and the jar is
    cleared on ``reset`` so a recycled client never leaks cookies across leases
    (session cookie state lives on the session, not the transport)."""

    kind = "http"

    def __init__(self, *, verify: bool = True, proxy: str | None = None) -> None:
        self._httpx = httpx.AsyncClient(
            follow_redirects=True, verify=verify, proxy=proxy
        )

    async def send(
        self,
        ref: Any,
        *,
        headers: dict[str, str],
        cookies: dict[str, str],
        timeout: float,
        retries: int = 0,
    ) -> httpx.Response:
        """Perform the request described by ``ref`` (retries transport errors)."""
        last: httpx.TransportError | None = None
        for _ in range(retries + 1):
            try:
                return await self._httpx.request(
                    ref.method.upper(),
                    ref.dispatch("url"),
                    headers=headers or None,
                    cookies=cookies or None,
                    content=ref.body,
                    json=ref.json_body,
                    data=ref.form,
                    follow_redirects=ref.follow_redirects,
                    timeout=ref.timeout if ref.timeout is not None else timeout,
                )
            except httpx.TransportError as exc:
                last = exc
        assert last is not None
        raise last

    async def reset(self) -> None:
        self._httpx.cookies.clear()

    async def aclose(self) -> None:
        await self._httpx.aclose()


class BrowserClient(Client):
    """One browser page (the leased unit for live documents)."""

    kind = "page"

    def __init__(self, page: Any) -> None:
        self.page = page

    async def aclose(self) -> None:
        await self.page.close()


class ClientFactory(abc.ABC):
    """Builds one kind of ``Client`` and owns that kind's shared resources."""

    kind: str

    @abc.abstractmethod
    async def create(self) -> Client: ...

    async def aclose(self) -> None:
        """Tear down factory-level resources (default: none)."""


class HTTPXFactory(ClientFactory):
    kind = "http"

    def __init__(self, *, verify: bool = True, proxy: str | None = None) -> None:
        self.verify = verify
        self.proxy = proxy

    async def create(self) -> HTTPXClient:
        return HTTPXClient(verify=self.verify, proxy=self.proxy)


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


__all__ = [
    "Client",
    "HTTPXClient",
    "BrowserClient",
    "ClientFactory",
    "HTTPXFactory",
    "BrowserFactory",
]
