"""Live page backing (M4): interaction on a real browser page.

A live ``Document`` carries a playwright ``_page``; this backing provides
the interaction set (``click`` / ``write`` / ``wait_for``), live selection, and
``reload``. Every op runs on the engine loop (where the browser lives) and
bridges back synchronously, so the eager surface stays sync. Console messages
and DOM mutations are captured onto the document as events (so ``console`` /
``dom_mutations`` / ``events_of`` and per-element narrowing work).

This backing owns the *whole* browser-capture concern: the injected JS
(``INIT_JS`` / ``DRAIN_JS``, declared as its ``page_scripts``), and the helpers
that turn raw page signals into events -- ``drain`` (DOM mutations), plus
``on_load`` wrapping the load-time console/network the client hands back. The
client just leases a page and fires ``on_load``; it never shapes events itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from ...clients import PageScript
from ...models import ActionEvent, ConsoleEvent, DOMUpdateEvent, NetworkEvent
from ..web_core import Backing

if TYPE_CHECKING:
    from ...clients import PageResult
    from ..client.loop import EngineLoop
    from . import Document
    from .html import HtmlBacking

#: installed on every navigation -- an id-path-tagging MutationObserver feeding
#: ``window.__wc_mutations`` (see ``LiveBacking.page_scripts``).
INIT_JS = """(() => {
  if (window.__wc_installed) return;
  window.__wc_installed = true;
  window.__wc_mutations = [];
  const MAIN = 'MAIN,ARTICLE,SECTION';
  new MutationObserver((muts) => {
    for (const m of muts) {
      let ids = []; let n = m.target; let inMain = false;
      while (n && n.nodeType === 1) {
        if (n.id) ids.push(n.id);
        if (MAIN.indexOf(n.tagName) >= 0 ||
            (n.getAttribute && n.getAttribute('role') === 'main')) inMain = true;
        n = n.parentElement;
      }
      window.__wc_mutations.push({type: m.type, ids: ids, inMain: inMain,
        added: m.addedNodes.length, removed: m.removedNodes.length});
    }
  }).observe(document,
             {childList: true, subtree: true, attributes: true, characterData: true});
  // At DOMContentLoaded (the served HTML parsed), discard the parse mutations and
  // record the text length THEN -- the baseline. Post-load injection is measured as
  // NET text GROWTH over it, so re-organising existing DOM (re-adds nodes but adds
  // no new text) is not mistaken for client-side rendering. (Net growth catches
  // shell-style SPAs; framework markers catch transform-style ones like AEM Edge,
  // whose text is replaced rather than grown.)
  const mark = () => {
    window.__wc_mutations = [];
    window.__wc_dcl_text = document.body ? (document.body.innerText || '').length : 0;
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mark, {once: true});
  } else { mark(); }
  // Fold shadow-DOM + same-origin iframe content into the LIGHT dom so page.content()
  // (and the skeleton the agent reads) contains the real records, not an empty
  // <custom-element>/<iframe> shell. Called from the "inline" page-script after settle.
  // Recurse into nested shadow roots first so deep trees are captured too.
  window.__wc_inline = () => {
    let shadow = 0, frames = 0;
    const pierce = (root) => {
      let n = 0;
      const hosts = root.querySelectorAll('*');
      for (const el of hosts) {
        if (el.shadowRoot) {
          n += 1 + pierce(el.shadowRoot);            // nested shadows first
          const holder = document.createElement('div');
          holder.setAttribute('data-wc-shadow', '');
          holder.innerHTML = el.shadowRoot.innerHTML;  // now includes inlined descendants
          el.appendChild(holder);
        }
      }
      return n;
    };
    try { shadow = pierce(document); } catch (e) {}
    for (const f of document.querySelectorAll('iframe')) {
      try {
        const idoc = f.contentDocument;               // null / throws for cross-origin
        if (idoc && idoc.body) {
          frames++;
          const holder = document.createElement('div');
          holder.setAttribute('data-wc-frame', f.getAttribute('src') || '');
          holder.innerHTML = idoc.body.innerHTML;
          f.parentNode.insertBefore(holder, f.nextSibling);
        }
      } catch (e) { /* cross-origin frame -- unreadable, leave it */ }
    }
    return {shadow: shadow, frames: frames};
  };
})()"""

#: read + clear the mutation buffer AND snapshot the settled page's total text +
#: element count + the DOMContentLoaded-baseline text (``dclText``) -- the runtime
#: facet measures injection as the NET text grown past that baseline. Run after
#: replay to discard load noise; on load-time capture the caller keeps the result
#: (see ``clients.browser.open``).
DRAIN_JS = """() => {
  const m = window.__wc_mutations || []; window.__wc_mutations = [];
  const t = document.body ? (document.body.innerText || '').length : 0;
  return {muts: m, text: t, nodes: document.getElementsByTagName('*').length,
          dclText: window.__wc_dcl_text || 0};
}"""

_LEVELS = {
    "log": "log",
    "info": "info",
    "debug": "log",
    "warning": "warning",
    "error": "error",
}


def _kind(record: dict[str, Any]) -> str:
    if record["type"] == "attributes":
        return "attribute"
    if record["type"] == "characterData":
        return "text"
    return "removed" if record["removed"] and not record["added"] else "added"


def _is_timeout(exc: BaseException) -> bool:
    """Whether ``exc`` is a Playwright timeout (its many timeout classes all end in
    ``TimeoutError``) -- the one place the stringly-typed heuristic lives."""
    return "Timeout" in type(exc).__name__


def _mutation_event(r: dict[str, Any], doc: "Document", *, phase: str | None = None) -> DOMUpdateEvent:
    """Wrap one raw mutation record into a DOMUpdateEvent, carrying the position /
    size detail the runtime facet reads (``inMain`` / ``added`` / ``addedText``)."""
    detail: dict[str, Any] = {
        "ids": r.get("ids", []),
        "inMain": bool(r.get("inMain")),
        "added": int(r.get("added", 0)),
    }
    if phase:
        detail["phase"] = phase
    return DOMUpdateEvent(kind=cast(Any, _kind(r)), detail=detail, document_id=doc.name)


async def drain(doc: "Document") -> None:
    """After an interaction: move any pending DOM mutations onto the document AND
    refresh its captured ``content`` from the (now-changed) live page, so a later
    ``select`` / ``text_content`` / ``skeleton`` -- including the in-memory fallback
    the plan evaluator takes on the engine loop -- sees the post-interaction DOM, not
    the original render. A short settle lets the observer deliver the last records."""
    await doc._page.wait_for_timeout(30)
    result = await doc._page.evaluate(DRAIN_JS)
    muts = result.get("muts", []) if isinstance(result, dict) else result
    for r in muts:
        doc._events.append(_mutation_event(r, doc))
    doc.content = (await doc._page.content()).encode()  # keep content current
    doc._tree = None  # invalidate the cached lxml parse of the old content


def console_event(level: str, text: str, doc: "Document") -> ConsoleEvent:
    return ConsoleEvent(
        level=cast(Any, _LEVELS.get(level, "log")), text=text, document_id=doc.name
    )


def network_event(method: str, url: str, resource_type: str, doc: "Document") -> NetworkEvent:
    """A browser sub-request captured onto the document (an XHR/fetch the page
    made) -- the raw material for the summary ``runtime`` facet's xhr_endpoints."""
    from ..reference import from_url

    return NetworkEvent(
        request=from_url(url, cast(Any, method.lower())),
        resource_type=resource_type,
        document_id=doc.name,
    )


def _html() -> "HtmlBacking":
    """The (stateless) HTML backing, for an in-memory select on captured content."""
    from .html import HtmlBacking

    return HtmlBacking()


class LiveBacking(Backing):
    """Interaction + live selection on a browser page (capability ``page``)."""

    provides = frozenset(
        {"click", "write", "wait_for", "select", "select_all", "evaluate", "screenshot"}
    )
    collections = frozenset({"select_all"})
    props = frozenset({"dom_mutations", "console"})
    #: the always-IO browser interactions -> awaitable under async. ``select`` /
    #: ``select_all`` are omitted: on a *static* document (the common case) they
    #: are in-memory (HtmlBacking), so the surface types them synchronously.
    io = frozenset({"click", "write", "wait_for", "evaluate", "screenshot"})
    #: the browser scripts this backing owns: the mutation observer (``init``,
    #: read by ``dom_mutations`` via ``drain``) and the buffer drain (``drain``
    #: phase, run after replay to discard load-time mutations). The client gathers
    #: and installs them; the backing owns the *what*.
    page_scripts = (
        PageScript(INIT_JS, "init"),
        # fold shadow-DOM / same-origin iframe content into the light DOM before the snapshot
        PageScript("() => window.__wc_inline ? window.__wc_inline() : {shadow:0,frames:0}", "inline"),
        PageScript(DRAIN_JS, "drain"),
    )
    gate = "page"

    def applies(self, core: "Document") -> bool:
        return core._page is not None

    def on_load(self, core: "Document", result: "PageResult") -> None:
        """Wrap the load-time console/network/DOM-mutations the client captured (a
        ``clients.PageResult``) into events on the document -- the client hands
        back raw facts and fires this; the backing owns the shaping."""
        for level, text in result.console:
            core._events.append(console_event(level, text, core))
        for method, url, rtype in result.network:  # XHR/fetch the page issued
            if rtype in ("xhr", "fetch"):
                core._events.append(network_event(method, url, rtype, core))
        # DOM mutations the page made during load/settle -- tagged phase="load" (with
        # position/size detail) so the runtime facet can measure how much content the
        # page composed after navigation, and where.
        for r in getattr(result, "mutations", []):
            core._events.append(_mutation_event(r, core, phase="load"))
        core._render_stats = getattr(result, "dom_stats", {}) or {}

    def _loop(self, core: "Document") -> "EngineLoop":
        return core._client.loop()

    # -- captured event views ------------------------------------------------
    def dom_mutations(self, core: "Document") -> "list[DOMUpdateEvent]":
        return [e for e in core._events if isinstance(e, DOMUpdateEvent)]

    def console(self, core: "Document") -> "list[ConsoleEvent]":
        return [e for e in core._events if isinstance(e, ConsoleEvent)]

    # -- interactions (IO: async def; the interface bridges via dispatch) -----
    async def click(
        self,
        core: "Document",
        selector: str | None = None,
        *,
        timeout: float | None = None,
        optional: bool = False,
        error: Any = None,
    ) -> "Document":
        from ...errors import lenient

        await self._aact(
            core, "click", selector=selector, timeout=timeout,
            optional=lenient(optional, error),
        )
        return core

    async def write(
        self,
        core: "Document",
        selector: str,
        text: str,
        *,
        timeout: float | None = None,
        optional: bool = False,
        error: Any = None,
    ) -> "Document":
        from ...errors import lenient

        await self._aact(
            core, "write", selector=selector, text=text, timeout=timeout,
            optional=lenient(optional, error),
        )
        return core

    async def wait_for(
        self,
        core: "Document",
        selector: str | None = None,
        *,
        timeout: float | None = None,
        optional: bool = False,
        error: Any = None,
    ) -> "Document":
        from ...errors import lenient, select_error

        optional = lenient(optional, error)
        try:
            await self._await_for(core, selector, timeout)
        except Exception as exc:  # a Playwright timeout -> structured miss (or lenient)
            if _is_timeout(exc):
                if optional:
                    return core
                raise select_error(f"wait_for: no {selector!r} within timeout") from exc
            raise
        return core

    # ``select`` / ``select_all`` are NOT ``io``: on a static document (the common
    # case) they are in-memory (HtmlBacking). On a live page they bridge to the
    # live DOM off the engine loop -- BUT when the caller is already ON the engine
    # loop (a plan run by the evaluator, e.g. ``wc.execute`` of a
    # ``resolve(browser="always").select(...)`` plan) that sync bridge is illegal
    # (it would block the loop on itself). There we fall back to an in-memory select
    # on the captured rendered content -- correct for a resolve->select plan, and it
    # keeps the page for any live interaction ops (which are ``io`` and await fine).
    def select(
        self,
        core: "Document",
        selector: str,
        *,
        index: int = 0,
        optional: bool = False,
        error: Any = None,
    ) -> "Document":
        from ...errors import RETURN

        if self._loop(core).on_loop_thread():
            return _html().select(
                core, selector, index=index, optional=optional, error=error
            )
        return self._loop(core).run(
            self._aselect(core, selector, index, RETURN if optional else error)
        )

    def select_all(
        self, core: "Document", selector: str, *, limit: int | None = None, offset: int = 0
    ) -> "list[Document]":
        if self._loop(core).on_loop_thread():
            return _html().select_all(core, selector, limit=limit, offset=offset)
        return self._loop(core).run(self._aselect_all(core, selector, limit=limit, offset=offset))

    async def evaluate(self, core: "Document", script: str, *, mutates: bool = True) -> Any:
        """Run ``script`` in the live page and return its result. ``mutates`` (default
        True) drains afterward -- refreshing the captured content and invalidating the
        cached tree -- so a later ``text_content`` / ``html`` / ``select`` reflects any
        DOM the script changed. Pass ``mutates=False`` for a pure read to skip the settle."""
        result = await core._page.evaluate(script)
        if mutates:
            await drain(core)
        return result

    async def screenshot(self, core: "Document", selector: str | None = None) -> "Document":
        return await self._ashot(core, selector)

    # -- async bodies --------------------------------------------------------
    async def _aact(
        self,
        core: "Document",
        action: str,
        *,
        selector: str | None = None,
        text: str | None = None,
        timeout: float | None = None,
        optional: bool = False,
    ) -> None:
        ms = (timeout or 30.0) * 1000
        event = ActionEvent(
            action=action,
            args={"selector": selector, "text": text},
            document_id=core.name,
            source="core-action",
        )
        core._client.bus.publish(event)
        core._events.append(event)  # routed onto the document
        if core._ref is not None:
            core._ref.actions.append(
                {
                    "op": action,
                    "args": {
                        k: v
                        for k, v in (("selector", selector), ("text", text))
                        if v is not None
                    },
                }
            )
        loc = core._page.locator(selector or "*").first
        try:
            if action == "click":
                await loc.click(timeout=ms)
            elif action == "write":
                await loc.fill(text or "", timeout=ms)
        except Exception as exc:
            if _is_timeout(exc):
                if optional:
                    return
                from ...errors import select_error

                raise select_error(f"{action}: no target for {selector!r}") from exc
            raise
        await drain(core)

    async def _await_for(
        self, core: "Document", selector: str | None, timeout: float | None
    ) -> None:
        if selector is not None:
            await core._page.wait_for_selector(
                selector, timeout=(timeout or 30.0) * 1000
            )
        elif timeout is not None:
            await core._page.wait_for_timeout(timeout * 1000)
        await drain(core)

    async def _ashot(self, core: "Document", selector: str | None) -> "Document":
        from . import Document

        target = core._page if selector is None else core._page.locator(selector).first
        data = await target.screenshot(type="png")
        shot = Document(
            url=core.url, kind="binary", content=data, status_code=core.status_code
        )
        shot._client = core._client
        return shot

    async def _aselect(self, core: "Document", selector: str, index: int, error: Any) -> "Document":
        from ...errors import RETURN
        from . import Document

        from ...errors import select_error

        loc = core._page.locator(selector)
        if await loc.count() <= index:
            if error is not RETURN:  # live select is loud by default
                raise select_error(f"no match for {selector!r}")
            sub = Document(url=core.url, kind="html", status_code=core.status_code)
            sub._client = core._client
            sub._missing = True
            return sub
        html = await loc.nth(index).evaluate("el => el.outerHTML")
        sub = Document(
            url=core.url,
            final_url=core.final_url,
            kind="html",
            content=html.encode(),
            status_code=core.status_code,
        )
        sub._client = core._client
        sub.root = core.name
        nid = selector[1:] if selector.startswith("#") and " " not in selector else None
        sub._events = [
            e
            for e in core._events
            if isinstance(e, DOMUpdateEvent)
            # load-time composition is a document-level SPA signal, not an
            # interaction on this element -- exclude it from per-element narrowing.
            and e.detail.get("phase") != "load"
            and (nid is None or nid in e.detail.get("ids", []))
        ]
        return sub

    async def _aselect_all(
        self, core: "Document", selector: str, *, limit: int | None = None, offset: int = 0
    ) -> "list[Document]":
        from . import Document

        loc = core._page.locator(selector)
        total = await loc.count()
        stop = total if limit is None else min(total, offset + limit)
        out: "list[Document]" = []
        for i in range(offset, stop):
            html = await loc.nth(i).evaluate("el => el.outerHTML")
            sub = Document(
                url=core.url,
                final_url=core.final_url,
                kind="html",
                content=html.encode(),
                status_code=core.status_code,
            )
            sub._client = core._client
            sub.root = core.name
            out.append(sub)
        return out


__all__ = [
    "LiveBacking",
    "INIT_JS",
    "DRAIN_JS",
    "drain",
    "console_event",
    "network_event",
]
