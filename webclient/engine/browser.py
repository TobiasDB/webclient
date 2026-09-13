"""Browser host: playwright lifecycle behind the page side of the pool.

One Browser per WebClient (launched lazily on the engine loop), one
BrowserContext per session (storage_state round-trips), one Page per page
lease. Everything here runs on the loop; the sync facade lives on
LiveDocument / WebClient.
"""

from __future__ import annotations

from typing import Any

_DEFAULT_CONTEXT = "__default__"


class BrowserHost:
    def __init__(self, headless: bool = True) -> None:
        self.headless = headless
        self._playwright: Any = None
        self._browser: Any = None
        self._contexts: dict[str, Any] = {}

    async def _ensure_browser(self) -> Any:
        if self._browser is None:
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless
            )
        return self._browser

    async def context_for(self, session: Any = None) -> Any:
        """The session's BrowserContext (its storage_state loaded on first
        use); one shared default context for session-less pages."""
        key = session.id if session is not None else _DEFAULT_CONTEXT
        context = self._contexts.get(key)
        if context is None:
            browser = await self._ensure_browser()
            state = session.storage_state if session is not None else None
            context = await browser.new_context(storage_state=state)
            self._contexts[key] = context
        return context

    async def new_page(self, session: Any = None) -> Any:
        context = await self.context_for(session)
        return await context.new_page()

    async def close_session_context(self, session: Any) -> None:
        """Persist storage_state back onto the session, then drop its
        context."""
        context = self._contexts.pop(session.id, None)
        if context is not None:
            try:
                session.storage_state = await context.storage_state()
            finally:
                await context.close()

    async def aclose(self) -> None:
        for context in self._contexts.values():
            try:
                await context.close()
            except Exception:
                pass
        self._contexts.clear()
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
