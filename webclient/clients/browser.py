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
from enum import Enum
from typing import Any, Literal

from ..errors import RAISE, RETURN, _Policy, select_error
from .base import Client, ClientFactory

Phase = Literal["init", "load", "drain"]


class WaitEvent(Enum):
    """The DOM/navigation milestone ``open`` waits for after navigating -- the
    controllable, self-describing replacement for the old opaque "wait for stable
    DOM". Pick the one that matches how the page delivers its content:

    * ``DOMCONTENTLOADED`` -- the served HTML is parsed (Playwright's
      ``domcontentloaded``). The cheapest wait: use it for a server-rendered page
      whose content is already in the initial HTML (no JS needed to see it).
    * ``LOAD`` -- the ``load`` event (sub-resources fetched too). Use it when the
      content you need depends on images/stylesheets/synchronous scripts having run.
    * ``NETWORKIDLE`` -- no network connections for ``quiet`` seconds. Use it for a
      page that fetches its data over XHR/fetch right after load and you want those
      responses in before snapshotting. Many sites never truly idle, so it is always
      bounded by ``timeout``.
    * ``DOM_STABLE`` -- the element count stops changing for ``quiet`` seconds (after
      a bounded network-idle nudge). The default and the safest general wait for a
      JS/SPA page: it returns the moment the page stops rewriting its own DOM, so a
      fast page costs little and a slow one is capped at ``timeout``. This is the
      historical "wait for stable DOM" made explicit.
    * ``SELECTOR`` -- a specific ``selector`` appears in the DOM. The most precise
      wait: use it when you know the one element that marks "the content I want is
      here" (e.g. ``.product-list``), so you neither under- nor over-wait.
    """

    LOAD = "load"
    DOMCONTENTLOADED = "domcontentloaded"
    NETWORKIDLE = "networkidle"
    DOM_STABLE = "dom_stable"
    SELECTOR = "selector"


@dataclass(frozen=True)
class WaitConfig:
    """How ``open`` waits for the page to be ready before snapshotting, and what to
    do if that wait times out.

    ``event`` picks the milestone (see :class:`WaitEvent`). ``timeout`` bounds the
    *whole* wait in seconds. ``quiet`` is the settle window for ``NETWORKIDLE`` /
    ``DOM_STABLE`` (how long "nothing changed" must hold). ``selector`` is the target
    for ``WaitEvent.SELECTOR``.

    ``on_timeout`` is the timeout policy, in the library's loud-by-default model:
    :data:`~webclient.errors.RAISE` (the default) raises a structured
    ``WebException`` when the awaited milestone is not reached in time;
    :data:`~webclient.errors.RETURN` instead returns what has rendered so far (the
    partial DOM). ``DOM_STABLE`` and ``NETWORKIDLE`` treat reaching the budget as a
    normal settle, not a failure, so they only consult ``on_timeout`` when the page
    is still actively mutating at the deadline; ``LOAD`` / ``DOMCONTENTLOADED`` /
    ``SELECTOR`` await a concrete condition, so a timeout there is a real miss."""

    event: WaitEvent = WaitEvent.DOM_STABLE
    timeout: float = 8.0
    quiet: float = 0.4
    selector: str | None = None
    on_timeout: _Policy = RAISE


#: the default wait: the historical lenient "settle the DOM" behaviour made
#: explicit -- poll until the DOM stops mutating, and never raise if it never fully
#: settles (a bounded best-effort, exactly as before).
DEFAULT_WAIT = WaitConfig(event=WaitEvent.DOM_STABLE, on_timeout=RETURN)


def _is_timeout(exc: BaseException) -> bool:
    """Whether ``exc`` is a Playwright timeout (its timeout classes all end in
    ``TimeoutError``) -- the stringly-typed heuristic, kept transport-local."""
    return "Timeout" in type(exc).__name__


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
    #: the REAL transport facts of the main navigation, read from Playwright's
    #: main-document ``Response`` (``page.goto`` return) -- so a browser-rendered
    #: document carries its true HTTP status + response headers, not a fabricated
    #: ``200`` / empty headers. ``status_code`` is ``0`` when Playwright reported no
    #: main response (e.g. ``about:blank`` or a same-document navigation).
    status_code: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    console: list[tuple[str, str]] = field(default_factory=list)  # (level, text)
    network: list[tuple[str, str, str]] = field(  # (method, url, resource_type)
        default_factory=list
    )
    #: DOM-mutation records the page accumulated during the initial load+settle (the
    #: drained observer buffer) -- how the page rewrote its own DOM after navigation.
    mutations: list[dict[str, Any]] = field(default_factory=list)
    #: the settled page's totals (``text`` chars, ``nodes``) -- the denominators for
    #: "what fraction of the content was injected after load".
    dom_stats: dict[str, Any] = field(default_factory=dict)


class BrowserClient(Client):
    """One browser page (the leased unit for live documents) -- it owns the actual
    browser driving; the live document ops (``LiveBacking``) then interact with
    ``self.page`` directly."""

    kind = "page"

    def __init__(self, page: Any) -> None:
        self.page = page

    async def _wait_stable(
        self, page: Any, *, timeout: float = 8.0, quiet: float = 0.4, poll: float = 0.2
    ) -> bool:
        """Wait for the DOM to settle so JS/lazy-loaded content is present before the
        snapshot: first let the network go idle (bounded -- many sites never truly
        idle), then poll the element count until it is unchanged for ``quiet`` seconds.
        Returns early the moment it's stable, so a page that settles quickly costs
        little; a JS page waits just until it stops mutating. ``timeout`` bounds the
        *whole* wait (the network-idle phase counts against it), so a page that never
        settles can never block longer than ``timeout``. Returns ``True`` if the DOM
        settled, ``False`` if the budget was exhausted while it was still mutating (so
        the caller can apply its timeout policy)."""
        import time as _time

        deadline = _time.monotonic() + timeout  # set first: the total budget
        idle_ms = min(timeout, 3.0) * 1000
        try:  # a bounded network-idle wait; ignore if it never idles
            await page.wait_for_load_state("networkidle", timeout=idle_ms)
        except Exception:
            pass
        need = max(1, round(quiet / poll))
        last, stable = -1, 0
        while _time.monotonic() < deadline:
            try:
                count = await page.evaluate("() => document.getElementsByTagName('*').length")
            except Exception:
                return True  # page went away -> nothing more to wait for
            if count == last:
                stable += 1
                if stable >= need:
                    return True
            else:
                last, stable = count, 0
            await page.wait_for_timeout(poll * 1000)
        return False  # deadline hit while still mutating

    async def _do_wait(self, page: Any, wait: WaitConfig) -> None:
        """Apply a :class:`WaitConfig` after navigation: wait for its milestone,
        bounded by ``wait.timeout``, then honour ``wait.on_timeout`` if the milestone
        is not reached (``RAISE`` -> a structured miss; ``RETURN`` -> the partial DOM).
        ``DOM_STABLE`` / ``NETWORKIDLE`` treat reaching the budget as a normal settle
        unless the page is still actively mutating at the deadline."""
        ms = wait.timeout * 1000
        try:
            if wait.event is WaitEvent.DOM_STABLE:
                settled = await self._wait_stable(
                    page, timeout=wait.timeout, quiet=wait.quiet
                )
                if not settled and wait.on_timeout is RAISE:
                    raise select_error(
                        f"wait: DOM still mutating after {wait.timeout}s"
                    )
                return
            if wait.event is WaitEvent.SELECTOR:
                if not wait.selector:
                    raise ValueError("WaitEvent.SELECTOR requires a selector")
                await page.wait_for_selector(wait.selector, timeout=ms)
                return
            # LOAD / DOMCONTENTLOADED / NETWORKIDLE map to a Playwright load state.
            await page.wait_for_load_state(wait.event.value, timeout=ms)
        except Exception as exc:
            if _is_timeout(exc) and wait.on_timeout is RETURN:
                return  # lenient: hand back what has rendered so far
            if _is_timeout(exc):
                raise select_error(
                    f"wait: {wait.event.value} not reached within {wait.timeout}s"
                ) from exc
            raise

    async def open(
        self,
        url: str,
        *,
        scripts: "tuple[PageScript, ...] | list[PageScript]" = (),
        replay: "list[dict[str, Any]]" = [],
        wait: "WaitConfig | None" = None,
    ) -> PageResult:
        """Navigate to ``url``, installing ``scripts`` by phase (``init`` before
        nav, ``load`` once after, ``drain`` after any replay), capturing the REAL
        main-response transport facts (status + headers + final URL), console +
        network requests, and replaying any recorded actions. Returns the raw page
        facts; the domain (document + events) is built by the caller.

        ``wait`` (a :class:`WaitConfig`, default :data:`DEFAULT_WAIT` -- settle the
        DOM) chooses which milestone to wait for and the timeout behaviour, so
        JS/lazy-loaded content (links, cards, …) is in the snapshot -- the fix for
        a render that captured the shell before the page finished loading."""
        page = self.page
        wait = wait or DEFAULT_WAIT
        for s in scripts:  # init scripts must be installed before navigation
            if s.phase == "init":
                await page.add_init_script(s.source)
        console: list[tuple[str, str]] = []
        page.on("console", lambda m: console.append((m.type, m.text)))
        network: list[tuple[str, str, str]] = []
        page.on("request", lambda r: network.append((r.method, r.url, r.resource_type)))
        # the main-document Response -- the REAL status/headers of the navigation
        # (Playwright hands it back from ``goto``). ``None`` for a non-HTTP nav.
        response = await page.goto(url, wait_until="domcontentloaded")
        await self._do_wait(page, wait)  # let JS/lazy content load before snapshotting
        status: int = 0
        headers: dict[str, str] = {}
        if response is not None:
            status = response.status
            try:  # all_headers() merges multi-value / continued headers
                headers = dict(await response.all_headers())
            except Exception:  # pragma: no cover - fall back to the sync view
                headers = dict(response.headers)
        # snapshot the settled console/network + the DOM mutations the page made
        # during load/settle (drained now, before any replay, so ``mutations`` is
        # the load-time rewrite -- how the page composed its own DOM).
        result = PageResult(
            page.url,
            (await page.content()).encode(),
            status_code=status,
            headers=headers,
            console=list(console),
            network=list(network),
        )
        for s in scripts:  # drain the load-time observer buffer -> result.mutations
            if s.phase == "drain":
                drained = await page.evaluate(s.source)
                if isinstance(drained, dict):  # {muts, text, nodes, dclText}
                    result.mutations.extend(drained.get("muts", []))
                    result.dom_stats = {
                        "text": drained.get("text", 0),
                        "nodes": drained.get("nodes", 0),
                        "dclText": drained.get("dclText", 0),
                    }
                elif isinstance(drained, list):  # back-compat
                    result.mutations.extend(drained)
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


__all__ = [
    "BrowserClient",
    "BrowserFactory",
    "PageScript",
    "PageResult",
    "Phase",
    "WaitEvent",
    "WaitConfig",
    "DEFAULT_WAIT",
]
