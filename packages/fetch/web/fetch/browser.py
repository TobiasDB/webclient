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

from . import mouse
from .bus import emit
from .chrome import BrowserSupply, supply_for
from .errors import classify
from .fingerprint import Fingerprint, as_fingerprint
from .models import (
    CaptureEvent,
    ConsoleEvent,
    DOMEvent,
    FetchEvent,
    NetworkEvent,
    Request,
    Script,
    Snapshot,
    Wait,
)
from .proxy import Proxy, as_proxy
from .script import ScriptRegistry, default_scripts
from .wait import apply_wait

if TYPE_CHECKING:  # playwright is an optional extra; imported lazily at runtime in _browser_ready
    from playwright.async_api import (
        Browser,
        BrowserContext,
        ConsoleMessage,
        Page,
        Playwright,
        Response,
    )

# A broad stealth patch -- only the FALLBACK when browserforge's injector is unavailable (the
# injector does this and much more, coherently). Papers over the loudest headless tells: the
# webdriver flag, an empty plugins/mimeTypes list, a missing window.chrome, and the headless WebGL
# vendor/renderer (SwiftShader) that anti-bot vendors key on.
_STEALTH = (
    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
    "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});"
    "window.chrome={runtime:{}};"
    "const gp=WebGLRenderingContext.prototype.getParameter;"
    "WebGLRenderingContext.prototype.getParameter=function(p){"
    "if(p===37445)return 'Intel Inc.';if(p===37446)return 'Intel Iris OpenGL Engine';"
    "return gp.call(this,p);};"
)
# A SUPPLEMENTAL patch applied on TOP of browserforge's injection (which spoofs UA / WebGL / canvas
# but leaves two loud automation tells): a real Chrome always exposes a populated
# `navigator.userAgentData.brands` and a non-empty `navigator.plugins` (the built-in PDF viewers).
# Headless/injected contexts leave both empty -- a dead giveaway a top-tier anti-bot keys on. This
# fills them coherently, deriving the brand version from the (already-spoofed) UA. Idempotent.
_STEALTH_SUPP = """
(() => {
  const m = navigator.userAgent.match(/Chrome\\/(\\d+)/);
  const v = m ? m[1] : '146';
  const brands = [
    {brand: 'Chromium', version: v},
    {brand: 'Google Chrome', version: v},
    {brand: 'Not.A/Brand', version: '24'},
  ];
  const plat = /Windows/.test(navigator.userAgent) ? 'Windows'
    : /Mac/.test(navigator.userAgent) ? 'macOS' : 'Linux';
  try {
    if (!navigator.userAgentData || !navigator.userAgentData.brands || !navigator.userAgentData.brands.length) {
      Object.defineProperty(navigator, 'userAgentData', {configurable: true, get: () => ({
        brands, mobile: false, platform: plat,
        getHighEntropyValues: async () => ({
          brands, mobile: false, platform: plat, platformVersion: '15.0.0',
          architecture: 'x86', bitness: '64', uaFullVersion: v + '.0.0.0', fullVersionList: brands,
        }),
      })});
    }
  } catch (e) {}
  try {
    if (!window.chrome) {  // real Chrome always exposes this; headless leaves it undefined
      window.chrome = {runtime: {}, loadTimes: function () {}, csi: function () {}, app: {}};
    }
  } catch (e) {}
  try {
    if (!navigator.plugins || navigator.plugins.length === 0) {
      const names = ['PDF Viewer', 'Chrome PDF Viewer', 'Chromium PDF Viewer',
                     'Microsoft Edge PDF Viewer', 'WebKit built-in PDF'];
      const arr = names.map(n => ({name: n, description: 'Portable Document Format',
                                   filename: 'internal-pdf-viewer', length: 1}));
      Object.defineProperty(navigator, 'plugins', {configurable: true, get: () => arr});
      Object.defineProperty(navigator, 'mimeTypes', {configurable: true, get: () =>
        [{type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format'}]});
    }
  } catch (e) {}
})();
"""
_BODY_CAP = 512_000  # per-response body captured (text/data responses only)
_BODIES_MAX = 60  # how many response bodies to drain per snapshot


def _bf_os(platform: str) -> str:
    """Map a fingerprint platform (``Windows`` / ``macOS`` / ``Linux``) to browserforge's OS token."""
    p = platform.lower()
    if "mac" in p or "darwin" in p:
        return "macos"
    if "linux" in p:
        return "linux"
    return "windows"


class BrowserSession:
    """A browser session: it OWNS a Playwright context + page. Actions mutate the page and return
    Self; ``snapshot`` materialises a Snapshot; ``aclose`` closes the context (and its page).
    """

    def __init__(
        self,
        context: "BrowserContext",
        page: "Page",
        scripts: tuple[Script, ...],
        wait: "Wait | None" = None,
    ) -> None:
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
        returns Self so navigation and actions chain. Installs the ``load`` recorders after render.
        """
        self._request = request
        self._responses.clear()
        w = wait or self._wait
        nav: Literal["domcontentloaded", "load"] = (
            "domcontentloaded" if w.until == "domcontentloaded" else "load"
        )
        resp = await self._page.goto(request.url, wait_until=nav, timeout=request.timeout * 1000)
        self._status = resp.status if resp is not None else 0
        self._headers = dict(resp.headers) if resp is not None else {}
        await apply_wait(self._page, w)  # settle further (networkidle / dom_stable / selector)
        for s in self._scripts:
            if s.on == "load":
                try:  # a load recorder is best-effort -- a hiccup must not discard a real response
                    await self._page.evaluate(s.js)
                except Exception:
                    pass
        return self

    async def click(self, selector: str, *, human: bool = False) -> "BrowserSession":
        """Click ``selector``. ``human=True`` moves the cursor there along a human path first
        (see :mod:`.mouse`) -- for a page that scores pointer behaviour."""
        if human:
            el = await self._page.query_selector(selector)
            box = await el.bounding_box() if el is not None else None
            if box:
                await mouse.move_along(
                    self._page,
                    box["x"] + box["width"] / 2,
                    box["y"] + box["height"] / 2,
                )
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
            out.append(
                NetworkEvent(
                    method=r.request.method,
                    url=r.url,
                    status=r.status,
                    resource_type=rtype,
                    body=body,
                )
            )
        return out

    async def snapshot(self) -> Snapshot:
        """Capture the current DOM as a Snapshot: the rendered HTML (with shadow roots + same-origin
        frames INLINED into the light DOM, so selectors reach web-component / iframe content), the
        network stream (with xhr/fetch bodies), console messages, and each recorder's drained DOM
        records."""
        for s in self._scripts:  # 'snapshot' transforms run BEFORE the HTML is read (deep DOM, ...)
            if s.on == "snapshot":
                try:
                    await self._page.evaluate(s.js)
                except Exception:  # never let a transform hiccup lose the snapshot
                    pass
        content: str = await self._page.content()
        emit(FetchEvent(url=self._page.url, status=self._status, source="browser"))
        events: list[CaptureEvent] = []
        events += await self._network_events()
        events += self._console
        for s in self._scripts:
            if s.drain:
                records = await self._page.evaluate(s.drain)
                if records:
                    events.append(DOMEvent(script=s.name, records=records))
        headers = self._headers or {"content-type": "text/html; charset=utf-8"}
        return Snapshot(
            request=self._request,
            url=self._page.url,
            status=self._status,
            headers=headers,
            content=content.encode("utf-8"),
            events=events,
        )

    async def fetch(self, request: Request) -> Snapshot:
        """Navigate + snapshot -- the Fetcher protocol within the session. Never raises: a nav
        failure becomes ``snapshot.error`` (classified)."""
        start = time.perf_counter()
        try:
            await self.goto(request)
        except Exception as exc:
            return Snapshot(
                request=request,
                url=request.url,
                elapsed=time.perf_counter() - start,
                error=classify(exc, url=request.url),
            )
        return await self.snapshot()

    async def aclose(self) -> None:
        """Close the context (and its page) -- the session owns them, so this is where they die."""
        await self._context.close()


class BrowserFetcher:
    """A browser backend over Playwright/Chromium (lazily launched). ``session()`` opens an owned
    :class:`BrowserSession`; ``fetch`` is a one-shot session."""

    def __init__(
        self,
        *,
        headless: bool = True,
        channel: str = "chromium",
        proxy: "str | Proxy | None" = None,
        fingerprint: "bool | Fingerprint" = False,
        cdp: "str | None" = None,
        wait: "Wait | None" = None,
        executable_path: "str | None" = None,
        supply: "BrowserSupply | None" = None,
        scripts: "tuple[Script, ...] | ScriptRegistry | None" = None,
    ) -> None:
        self._headless = headless
        self._channel = channel  # "chromium" = bundled; "chrome" = the real Chrome install
        #: an explicit browser BINARY to launch (a driver/executable path), instead of the one the
        #: channel resolves to -- for a pinned/self-managed Chromium or a custom build.
        self._executable = executable_path
        self._proxy = as_proxy(proxy)
        self._fingerprint = as_fingerprint(fingerprint)
        self._wait = wait  # the default readiness milestone for every session's goto
        #: a CDP endpoint (e.g. ``http://localhost:9222``) to ATTACH to an already-running browser
        #: instead of launching one -- a real user profile, a remote grid, an inspected Chrome.
        self._cdp = cdp
        #: HOW the browser is obtained (launch a local process vs attach over CDP) + its lifecycle,
        #: factored into :mod:`.chrome`. A caller can pass an explicit ``supply`` (e.g. a remote-CDP
        #: box on a residential exit); otherwise it is derived from the flat kwargs above.
        self._supply: BrowserSupply = supply or supply_for(
            cdp=cdp,
            headless=headless,
            channel=channel,
            executable_path=executable_path,
            proxy=self._proxy,
        )
        #: a registry so a caller can enable/disable capture scripts; a bare tuple is wrapped.
        self.scripts: ScriptRegistry = (
            default_scripts()
            if scripts is None
            else (scripts if isinstance(scripts, ScriptRegistry) else ScriptRegistry(scripts))
        )
        self._pw: "Playwright | None" = None
        self._browser: "Browser | None" = None

    async def _browser_ready(self) -> "Browser":
        browser = self._browser
        if browser is None:
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()
            # the supply obtains the browser -- launch a local process (with the real-browser launch
            # args) or attach to a running one over CDP; see :mod:`.chrome`.
            browser = await self._supply.connect(self._pw)
            self._browser = browser
        return browser

    async def session(self) -> BrowserSession:
        """Open a session that OWNS a fresh context + page (isolated cookies/state). Builds a REAL
        fingerprinted context (browserforge injects a coherent navigator + screen + WebGL vendor +
        canvas noise + plugins/fonts -- the surface anti-bot vendors probe) and installs the ``init``
        scripts before navigation.
        """
        browser = await self._browser_ready()
        context = await self._new_context(browser)
        page = await context.new_page()
        scripts = self.scripts.enabled()  # only the enabled scripts install
        try:
            if self._fingerprint is not None:  # fill the tells the injector leaves (brands/plugins)
                await page.add_init_script(_STEALTH_SUPP)
            for s in scripts:  # 'init' scripts run before any page script
                if s.on == "init":
                    await page.add_init_script(s.js)
        except BaseException:  # setup failed -> don't leak the context we opened
            await context.close()
            raise
        return BrowserSession(context, page, scripts, self._wait)

    async def _new_context(self, browser: "Browser") -> "BrowserContext":
        """A fresh context carrying the fingerprint. When one is set, browserforge's Playwright
        injector spoofs the FULL fingerprinting surface -- navigator (webdriver, plugins, languages,
        hardwareConcurrency), screen, WebGL vendor/renderer, canvas, audio, fonts -- so the page
        looks like a real user's Chrome to a canvas/WebGL fingerprinting probe, not a headless
        harness. Falls back to a coherent manual context + a stealth patch if the injector is
        unavailable, or a bare context when there is no fingerprint."""
        fp = self._fingerprint
        if fp is None:
            return await browser.new_context()
        try:
            from browserforge.injectors.playwright import (  # type: ignore[attr-defined]
                AsyncNewContext,
            )
        except ImportError:
            AsyncNewContext = None  # type: ignore[assignment]
        if AsyncNewContext is not None:  # the real deal: full coherent fingerprint injection
            opts = {"browser": ("chrome",), "os": (_bf_os(fp.platform),), "device": ("desktop",)}
            return await AsyncNewContext(browser, fingerprint_options=opts)
        w, h = fp.viewport  # fallback: a coherent manual context + a broad stealth patch
        context = await browser.new_context(
            user_agent=fp.user_agent, viewport={"width": w, "height": h}, locale=fp.locale
        )
        await context.add_init_script(_STEALTH)
        return context

    async def fetch(self, request: Request) -> Snapshot:
        """One-shot: open a session, fetch, close. Never raises (a session failure is
        ``snapshot.error``); the session -- and its page -- is always closed."""
        start = time.perf_counter()
        try:
            s = await self.session()
        except Exception as exc:
            return Snapshot(
                request=request,
                url=request.url,
                elapsed=time.perf_counter() - start,
                error=classify(exc, url=request.url),
            )
        try:
            return await s.fetch(request)
        finally:
            await s.aclose()

    async def aclose(self) -> None:
        if self._browser is not None:
            # only CLOSE a browser we launched; an attached (CDP) supply must leave the user's /
            # remote process running -- we just disconnect the driver by stopping Playwright below.
            if self._supply.owns_process:
                await self._browser.close()
            self._browser = None  # idempotent
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None


__all__ = ["BrowserFetcher", "BrowserSession"]
