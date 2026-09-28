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
from typing import TYPE_CHECKING, Literal

from web.kernel import Event, emit

from . import mouse
from .errors import classify
from .events import ConsoleEvent, DOMEvent, FetchEvent, NetworkEvent
from .fingerprint import Fingerprint, as_fingerprint
from .proxy import Proxy, as_proxy
from .request import Request
from .script import Script, ScriptRegistry, default_scripts
from .snapshot import Snapshot
from .wait import Wait, apply_wait

if TYPE_CHECKING:  # playwright is an optional extra; imported lazily at runtime in _browser_ready
    from playwright.async_api import Browser, BrowserContext, ConsoleMessage, Page, Playwright, Response

_STEALTH = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
_BODY_CAP = 512_000  # per-response body captured (text/data responses only)
_BODIES_MAX = 60     # how many response bodies to drain per snapshot


class BrowserSession:
    """A browser session: it OWNS a Playwright context + page. Actions mutate the page and return
    Self; ``snapshot`` materialises a Snapshot; ``aclose`` closes the context (and its page)."""

    def __init__(self, context: "BrowserContext", page: "Page", scripts: tuple[Script, ...],
                 wait: "Wait | None" = None) -> None:
        self._context = context
        self._page = page
        self._scripts = scripts
        self._wait = wait or Wait()
        self._request = Request(url=page.url or "about:blank")
        self._status = 0
        self._headers: dict[str, str] = {}
        self._responses: "list[Response]" = []  # raw Response objects; bodies drained in snapshot()
        self._console: list[ConsoleEvent] = []
        page.on("response", self._record_response)
        page.on("console", self._record_console)

    def _record_response(self, response: "Response") -> None:
        self._responses.append(response)

    def _record_console(self, message: "ConsoleMessage") -> None:
        self._console.append(ConsoleEvent(level=message.type, text=message.text))

    async def goto(self, request: Request, *, wait: "Wait | None" = None) -> "BrowserSession":
        """Navigate the owned page to ``request`` and settle per ``wait`` (else the session default);
        returns Self so navigation and actions chain. Installs the ``load`` recorders after render."""
        self._request = request
        self._responses.clear()
        w = wait or self._wait
        nav: Literal["domcontentloaded", "load"] = "domcontentloaded" if w.until == "domcontentloaded" else "load"
        resp = await self._page.goto(request.url, wait_until=nav, timeout=request.timeout * 1000)
        self._status = resp.status if resp is not None else 0
        self._headers = dict(resp.headers) if resp is not None else {}
        await apply_wait(self._page, w)  # settle further (networkidle / dom_stable / selector)
        for s in self._scripts:
            if s.on == "load":
                await self._page.evaluate(s.js)
        return self

    async def click(self, selector: str, *, human: bool = False) -> "BrowserSession":
        """Click ``selector``. ``human=True`` moves the cursor there along a human path first
        (see :mod:`.mouse`) -- for a page that scores pointer behaviour."""
        if human:
            el = await self._page.query_selector(selector)
            box = await el.bounding_box() if el is not None else None
            if box:
                await mouse.move_along(self._page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        await self._page.click(selector)
        return self

    async def type(self, selector: str, text: str) -> "BrowserSession":
        await self._page.fill(selector, text)
        return self

    async def wait_for(self, selector: str) -> "BrowserSession":
        await self._page.wait_for_selector(selector)
        return self

    async def scroll(self, selector: "str | None" = None) -> "BrowserSession":
        """Scroll ``selector`` into view, or the page to its bottom (to trigger lazy/infinite load)."""
        if selector is not None:
            await self._page.eval_on_selector(selector, "el => el.scrollIntoView()")
        else:
            await self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        return self

    async def back(self) -> "BrowserSession":
        await self._page.go_back()
        return self

    async def evaluate(self, script: str) -> object:
        """Run JS in the page and return its result -- a read, not a chainable action. The result is
        arbitrary JS, so it is typed ``object``; a caller narrows it."""
        return await self._page.evaluate(script)

    async def screenshot(self, selector: "str | None" = None) -> bytes:
        """A PNG of the page (or one element) -- returns the bytes; not chainable."""
        if selector is not None:
            el = await self._page.query_selector(selector)
            png: bytes = await el.screenshot() if el is not None else b""
            return png
        shot: bytes = await self._page.screenshot()
        return shot

    async def _network_events(self) -> "list[NetworkEvent]":
        """Build a NetworkEvent per response, draining xhr/fetch bodies (bounded, best-effort)."""
        out: list[NetworkEvent] = []
        drained = 0
        for r in self._responses:
            rtype = r.request.resource_type
            body = b""
            if rtype in ("xhr", "fetch") and drained < _BODIES_MAX:
                try:
                    raw = await r.body()
                    if raw and len(raw) <= _BODY_CAP:
                        body = raw
                    drained += 1
                except Exception:  # body gone / stream consumed -> skip, don't fail the snapshot
                    pass
            out.append(NetworkEvent(method=r.request.method, url=r.url, status=r.status,
                                    resource_type=rtype, body=body))
        return out

    async def snapshot(self) -> Snapshot:
        """Capture the current DOM as a Snapshot: the rendered HTML, the network stream (with
        xhr/fetch bodies), console messages, and each recorder's drained DOM records."""
        content: str = await self._page.content()
        emit(FetchEvent(url=self._page.url, status=self._status, source="browser"))
        events: list[Event] = []
        events += await self._network_events()
        events += self._console
        for s in self._scripts:
            if s.drain:
                records = await self._page.evaluate(s.drain)
                if records:
                    events.append(DOMEvent(script=s.name, records=records))
        headers = self._headers or {"content-type": "text/html; charset=utf-8"}
        return Snapshot(request=self._request, url=self._page.url, status=self._status,
                        headers=headers, content=content.encode("utf-8"), events=events)

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
        proxy: "str | Proxy | None" = None, fingerprint: "bool | Fingerprint" = False,
        cdp: "str | None" = None, wait: "Wait | None" = None,
        scripts: "tuple[Script, ...] | ScriptRegistry | None" = None,
    ) -> None:
        self._headless = headless
        self._channel = channel  # "chromium" = bundled; "chrome" = the real Chrome install
        self._proxy = as_proxy(proxy)
        self._fingerprint = as_fingerprint(fingerprint)
        self._wait = wait  # the default readiness milestone for every session's goto
        #: a CDP endpoint (e.g. ``http://localhost:9222``) to ATTACH to an already-running browser
        #: instead of launching one -- a real user profile, a remote grid, an inspected Chrome.
        self._cdp = cdp
        #: a registry so a caller can enable/disable capture scripts; a bare tuple is wrapped.
        self.scripts: ScriptRegistry = (
            default_scripts() if scripts is None
            else scripts if isinstance(scripts, ScriptRegistry)
            else ScriptRegistry(scripts)
        )
        self._pw: "Playwright | None" = None
        self._browser: "Browser | None" = None

    async def _browser_ready(self) -> "Browser":
        browser = self._browser
        if browser is None:
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()
            if self._cdp is not None:  # attach to an existing browser over the DevTools protocol
                browser = await self._pw.chromium.connect_over_cdp(self._cdp)
            else:
                browser = await self._pw.chromium.launch(
                    headless=self._headless,
                    channel=None if self._channel == "chromium" else self._channel,
                    proxy=self._proxy.playwright() if self._proxy else None,
                )
            self._browser = browser
        return browser

    async def session(self) -> BrowserSession:
        """Open a session that OWNS a fresh context + page (isolated cookies/state). Applies the
        fingerprint's context options + stealth pass and any ``init`` scripts before navigation."""
        browser = await self._browser_ready()
        options = self._fingerprint.context_options() if self._fingerprint else {}
        context = await browser.new_context(**options)
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
        return BrowserSession(context, page, scripts, self._wait)

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
