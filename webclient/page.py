"""`PageBacking` — the browser half of the backing contract.

Everything the surfaces need is expressed in the same address-based
primitives the tree backing implements, so `select`, `attr`, `render` and the
whole expression language work unchanged over a live page. Addresses here are
stable node ids assigned by an injected script (`nid:n17`), which is also what
makes them usable as playwright locators for interaction.

Assigning ids writes a `data-wc-id` attribute into the page. That is the price
of a stable address on a mutating DOM; it is visible in snapshots.
"""
from __future__ import annotations

import asyncio
from typing import Any, Literal, Sequence

from .backing import BROWSER, TREE, Backing, Kind, StaticBacking, is_xpath
from .errors import SelectionError, WebClientError
from .telemetry import ConsoleRecord, RequestRecord, Telemetry

_RUNTIME = r"""
(() => {
  if (window.__wc) return;
  const wc = window.__wc = { seq: 0 };
  wc.idOf = (el) => {
    if (!el || el.nodeType !== 1) return null;
    if (!el.dataset.wcId) el.dataset.wcId = "n" + (++wc.seq);
    return el.dataset.wcId;
  };
  wc.nodeOf = (id) =>
    id ? document.querySelector('[data-wc-id="' + id + '"]') : document;
  wc.query = (rootId, selector, limit, offset) => {
    const root = wc.nodeOf(rootId);
    if (!root) return null;
    let found;
    if (selector.startsWith("/") || selector.startsWith("./")) {
      const base = root === document ? document : root;
      const it = document.evaluate(selector, base, null,
        XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
      found = [];
      for (let i = 0; i < it.snapshotLength; i++) found.push(it.snapshotItem(i));
      if (found.some((n) => !n || n.nodeType !== 1)) return "NON_ELEMENT";
    } else {
      found = Array.from(root.querySelectorAll(selector));
    }
    const end = limit === null ? undefined : offset + limit;
    return found.slice(offset, end).map(wc.idOf);
  };
  wc.count = (rootId, selector) => {
    const out = wc.query(rootId, selector, null, 0);
    return out === null ? null : (out === "NON_ELEMENT" ? -1 : out.length);
  };
  wc.attr = (id, name) => {
    if (name === "body" && !id) return document.documentElement.outerHTML;
    const node = wc.nodeOf(id);
    if (!node) return { missing: true };
    if (name === "title" && !id) return document.title || null;
    const el = node === document ? document.documentElement : node;
    if (name === "text") return (el.innerText || el.textContent || "")
      .replace(/\s+/g, " ").trim();
    if (name === "html") return el.outerHTML;
    const value = el.getAttribute(name);
    return value === null ? { missing: true } : value;
  };
  wc.quiet = (ms, deadline) => new Promise((resolve) => {
    let timer = null;
    const observer = new MutationObserver(() => {
      clearTimeout(timer);
      timer = setTimeout(done, ms);
    });
    const done = () => { observer.disconnect(); clearTimeout(cap); resolve(true); };
    const cap = setTimeout(() => { observer.disconnect(); clearTimeout(timer);
      resolve(false); }, deadline);
    timer = setTimeout(done, ms);
    observer.observe(document.documentElement,
      { subtree: true, childList: true, attributes: true, characterData: true });
  });
})();
"""


class BrowserHost:
    """One browser per client, one context per session."""

    def __init__(self, headless: bool = True) -> None:
        self.headless = headless
        self._playwright: Any = None
        self._browser: Any = None
        self._contexts: dict[str, Any] = {}

    async def _browser_instance(self) -> Any:
        if self._browser is None:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless)
        return self._browser

    async def context_for(self, session: Any = None) -> Any:
        key = session.id if session is not None else "__default__"
        context = self._contexts.get(key)
        if context is None:
            browser = await self._browser_instance()
            state = session.storage_state if session is not None else None
            options: dict[str, Any] = {}
            if state:
                options["storage_state"] = state
            if session is not None and session.proxy is not None:
                options["proxy"] = {"server": session.proxy.url,
                                    "username": session.proxy.username,
                                    "password": session.proxy.password}
            context = await browser.new_context(**options)
            await context.add_init_script(_RUNTIME)
            self._contexts[key] = context
        return context

    async def new_page(self, session: Any = None) -> Any:
        return await (await self.context_for(session)).new_page()

    async def close_context(self, session: Any) -> None:
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


class PageBacking(Backing):
    """A live page. Capabilities: `tree` and `browser`."""

    name = "browser"
    capabilities = frozenset({TREE, BROWSER})

    def __init__(self, page: Any, *, lease: Any = None,
                 telemetry: Telemetry | None = None, request: Any = None,
                 timeout: float = 30.0) -> None:
        super().__init__()
        self.page = page
        self.lease = lease
        self.telemetry = telemetry or Telemetry()
        self.request = request
        self.timeout = timeout
        self.kind: Kind = "html"
        self._listeners: list[tuple[str, Any]] = []

    # -- setup ---------------------------------------------------------------
    async def prepare(self, session: Any, scripts: Sequence[Any]) -> None:
        for script in scripts:
            await self.page.add_init_script(script.source)
        headers = dict(session.headers) if session is not None else {}
        if self.request is not None and self.request.headers:
            headers.update(self.request.headers)
        if headers:
            await self.page.set_extra_http_headers(headers)
        if session is not None and session.cookies and self.request is not None:
            await self.page.context.add_cookies([
                {"name": name, "value": value, "url": self.request.url}
                for name, value in session.cookies.items()])
        self._listen()

    def _listen(self) -> None:
        def on_response(response: Any) -> None:
            self.telemetry.add(RequestRecord(
                url=response.url, method=response.request.method.lower(),
                status=response.status,
                kind=response.request.resource_type or "document"))

        def on_console(message: Any) -> None:
            self.telemetry.add(ConsoleRecord(level=message.type,
                                             text=message.text))

        self.page.on("response", on_response)
        self.page.on("console", on_console)
        self._listeners = [("response", on_response), ("console", on_console)]

    async def goto(self, url: str, *, wait_until: str = "load") -> int:
        response = await self.page.goto(url, wait_until=wait_until,
                                        timeout=self.timeout * 1000)
        self.final_url = self.page.url
        if response is not None:
            self.status_code = response.status
            self.response_headers = dict(await response.all_headers())
        else:
            self.status_code = 200
        self.content = (await self.page.content()).encode()
        return self.status_code

    async def sync_cookies(self, session: Any) -> None:
        for cookie in await self.page.context.cookies():
            session.cookies[cookie["name"]] = cookie["value"]

    # -- selection primitives -------------------------------------------------
    async def select_paths(self, root: str | None, selector: str, *,
                           limit: int | None = None,
                           offset: int = 0) -> list[str]:
        found = await self.page.evaluate(
            "a => window.__wc.query(a.root, a.selector, a.limit, a.offset)",
            {"root": _bare(root), "selector": selector, "limit": limit,
             "offset": offset})
        if found == "NON_ELEMENT":
            raise SelectionError(
                f"XPath {selector!r} selects non-elements; selection returns "
                "elements only — use .attr() for values")
        if found is None:
            raise SelectionError(f"element address {root!r} no longer resolves")
        return [f"nid:{node_id}" for node_id in found]

    async def count_paths(self, root: str | None, selector: str) -> int:
        found = await self.page.evaluate(
            "a => window.__wc.count(a.root, a.selector)",
            {"root": _bare(root), "selector": selector})
        return int(found or 0)

    async def attr_at(self, path: str | None, name: str) -> Any:
        value = await self.page.evaluate(
            "a => window.__wc.attr(a.id, a.name)",
            {"id": _bare(path), "name": name})
        if isinstance(value, dict) and value.get("missing"):
            return None
        return value

    def tree(self) -> Any:
        """A parsed snapshot, for renderers. Selection uses the live DOM."""
        from lxml import html as lxml_html
        return lxml_html.fromstring(self.content or b"<html></html>")

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    # -- interaction ----------------------------------------------------------
    def _locator(self, path: str | None, selector: str | None) -> Any:
        if selector:
            target = (f"xpath={selector}" if is_xpath(selector) else selector)
            scope = (self.page.locator(f'[data-wc-id="{_bare(path)}"]')
                     if path else self.page)
            return scope.locator(target)
        if path:
            return self.page.locator(f'[data-wc-id="{_bare(path)}"]')
        raise SelectionError("this action needs a selector or an element")

    async def act(self, action: str, path: str | None, selector: str | None,
                  **args: Any) -> None:
        timeout = (args.get("timeout") or self.timeout) * 1000
        optional = bool(args.get("optional"))
        try:
            await self._perform(action, path, selector, timeout, args)
        except Exception as exc:
            if optional and "Timeout" in type(exc).__name__:
                return
            if "Timeout" in type(exc).__name__:
                raise SelectionError(
                    f"{action}: no target for {selector or path!r} within "
                    f"{timeout / 1000:.1f}s") from exc
            raise
        self.content = (await self.page.content()).encode()
        self.final_url = self.page.url

    async def _perform(self, action: str, path: str | None,
                       selector: str | None, timeout: float,
                       args: dict[str, Any]) -> None:
        if action == "scroll" and not (selector or path):
            await self.page.evaluate("d => window.scrollBy(d.x, d.y)",
                                     {"x": args.get("x", 0),
                                      "y": args.get("y", 0)})
            return
        if action == "press" and not (selector or path):
            await self.page.keyboard.press(args["key"])
            return
        locator = self._locator(path, selector).first
        if action == "click":
            await locator.click(button=args.get("button", "left"),
                                click_count=args.get("count", 1),
                                timeout=timeout)
        elif action == "write":
            if args.get("clear", True):
                await locator.fill(args.get("text", ""), timeout=timeout)
            else:
                await locator.press_sequentially(
                    args.get("text", ""), delay=args.get("delay_ms"),
                    timeout=timeout)
        elif action == "press":
            await locator.press(args["key"], timeout=timeout)
        elif action == "hover":
            await locator.hover(timeout=timeout)
        elif action == "check":
            if args.get("checked", True):
                await locator.check(timeout=timeout)
            else:
                await locator.uncheck(timeout=timeout)
        elif action == "select_option":
            await locator.select_option(value=args.get("value"),
                                        label=args.get("label"),
                                        index=args.get("index"),
                                        timeout=timeout)
        elif action == "upload":
            await locator.set_input_files(args.get("files") or [],
                                          timeout=timeout)
        elif action == "scroll":
            await locator.evaluate("(el, d) => el.scrollBy(d.x, d.y)",
                                   {"x": args.get("x", 0),
                                    "y": args.get("y", 0)})
        else:
            raise WebClientError(f"unknown action {action!r}")

    # -- dom ------------------------------------------------------------------
    async def wait_stable(self, *, quiet_ms: int = 500,
                          timeout: float | None = None) -> bool:
        deadline = (timeout or self.timeout) * 1000
        settled = await self.page.evaluate(
            "a => window.__wc.quiet(a.quiet, a.deadline)",
            {"quiet": quiet_ms, "deadline": deadline})
        self.content = (await self.page.content()).encode()
        return bool(settled)

    async def wait_for(self, selector: str | None, *, state: str = "visible",
                       event: str | None = None,
                       timeout: float | None = None,
                       optional: bool = False) -> None:
        ms = (timeout or self.timeout) * 1000
        try:
            if selector is not None:
                target = (f"xpath={selector}" if is_xpath(selector)
                          else selector)
                await self.page.wait_for_selector(target, state=state,
                                                  timeout=ms)
            elif event is not None:
                await self.page.wait_for_load_state(event, timeout=ms)
            else:
                await asyncio.sleep((timeout or 0))
        except Exception as exc:
            if optional and "Timeout" in type(exc).__name__:
                return
            raise SelectionError(
                f"wait_for: {selector!r} not {state} within "
                f"{ms / 1000:.1f}s") from exc
        self.content = (await self.page.content()).encode()

    async def evaluate(self, script: str, path: str | None = None) -> Any:
        if path:
            return await self._locator(path, None).first.evaluate(script)
        return await self.page.evaluate(script)

    async def screenshot(self, path: str | None, selector: str | None, *,
                         full_page: bool = False,
                         format: str = "png") -> bytes:
        if selector or path:
            return await self._locator(path, selector).first.screenshot(
                type=format)
        return await self.page.screenshot(full_page=full_page, type=format)

    # -- freezing --------------------------------------------------------------
    async def snapshot(self) -> StaticBacking:
        """The static half of this Document, kept when the page moves on."""
        try:
            content = (await self.page.content()).encode()
        except Exception:
            content = self.content
        for event, handler in self._listeners:
            try:
                self.page.remove_listener(event, handler)
            except Exception:
                pass
        self._listeners = []
        frozen = StaticBacking(
            content, self.kind, status_code=self.status_code,
            final_url=self.final_url,
            response_headers=dict(self.response_headers),
            elapsed=self.elapsed, message=self.message)
        frozen.telemetry = self.telemetry            # type: ignore[attr-defined]
        return frozen


def _bare(path: str | None) -> str | None:
    if path is None:
        return None
    scheme, _, value = path.partition(":")
    if scheme != "nid":
        raise SelectionError(f"not a page address: {path!r}")
    return value
