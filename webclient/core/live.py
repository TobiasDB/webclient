"""Live page backing (M4): interaction on a real browser page.

A live ``DocumentCore`` carries a playwright ``_page``; this backing provides
the interaction set (``click`` / ``write`` / ``wait_for``), live selection, and
``reload``. Every op runs on the engine loop (where the browser lives) and
bridges back synchronously, so the eager surface stays sync. Console messages
and DOM mutations are captured onto the document as events (so ``console`` /
``dom_mutations`` / ``events_of`` and per-element narrowing work).
"""
from __future__ import annotations

from typing import Any

from ..events import ActionEvent, ConsoleEvent, DOMUpdateEvent
from .web_core import Backing

#: installed on every navigation (before page scripts) -- an id-path-tagging
#: MutationObserver feeding ``window.__wc_mutations``.
INIT_JS = """(() => {
  if (window.__wc_installed) return;
  window.__wc_installed = true;
  window.__wc_mutations = [];
  new MutationObserver((muts) => {
    for (const m of muts) {
      let ids = []; let n = m.target;
      while (n && n.nodeType === 1) { if (n.id) ids.push(n.id); n = n.parentElement; }
      window.__wc_mutations.push({type: m.type, ids: ids,
        added: m.addedNodes.length, removed: m.removedNodes.length});
    }
  }).observe(document,
             {childList: true, subtree: true, attributes: true, characterData: true});
})()"""

_DRAIN_JS = "() => { const m = window.__wc_mutations || []; window.__wc_mutations = []; return m; }"
_LEVELS = {"log": "log", "info": "info", "debug": "log",
           "warning": "warning", "error": "error"}


def _kind(record: dict[str, Any]) -> str:
    if record["type"] == "attributes":
        return "attribute"
    if record["type"] == "characterData":
        return "text"
    return "removed" if record["removed"] and not record["added"] else "added"


async def drain(doc: Any) -> None:
    """Move any pending DOM mutations off the page onto the document. A short
    settle lets the observer's microtask deliver records from the last action."""
    await doc._page.wait_for_timeout(30)
    for r in await doc._page.evaluate(_DRAIN_JS):
        doc._events.append(DOMUpdateEvent(
            kind=_kind(r), detail={"ids": r["ids"]}, document_id=doc.name))


def console_event(level: str, text: str, doc: Any) -> ConsoleEvent:
    return ConsoleEvent(level=_LEVELS.get(level, "log"), text=text,
                        document_id=doc.name)


class LiveBacking(Backing):
    """Interaction + live selection on a browser page (capability ``page``)."""

    provides = frozenset({"click", "write", "wait_for", "select", "select_all"})
    props = frozenset({"dom_mutations", "console"})
    gate = "page"

    def applies(self, core: Any) -> bool:
        return core._page is not None

    def _loop(self, core: Any) -> Any:
        return core._client.loop()

    # -- captured event views ------------------------------------------------
    def dom_mutations(self, core: Any) -> list[Any]:
        return [e for e in core._events if isinstance(e, DOMUpdateEvent)]

    def console(self, core: Any) -> list[Any]:
        return [e for e in core._events if isinstance(e, ConsoleEvent)]

    # -- interactions (sync; bridge onto the engine loop) --------------------
    def click(self, core: Any, selector: str | None = None, *,
              timeout: float | None = None) -> Any:
        self._loop(core).run(self._aact(core, "click", selector=selector, timeout=timeout))
        return core

    def write(self, core: Any, selector: str, text: str, *,
              timeout: float | None = None) -> Any:
        self._loop(core).run(self._aact(core, "write", selector=selector,
                                        text=text, timeout=timeout))
        return core

    def wait_for(self, core: Any, selector: str, *,
                 timeout: float | None = None) -> Any:
        self._loop(core).run(self._await_for(core, selector, timeout))
        return core

    def select(self, core: Any, selector: str, *, index: int = 0,
               error: Any = None) -> Any:
        return self._loop(core).run(self._aselect(core, selector, index, error))

    def select_all(self, core: Any, selector: str) -> Any:
        return self._loop(core).run(self._aselect_all(core, selector))

    # -- async bodies --------------------------------------------------------
    async def _aact(self, core: Any, action: str, *, selector: str | None = None,
                    text: str | None = None, timeout: float | None = None) -> None:
        ms = (timeout or 30.0) * 1000
        core._client.bus.publish(ActionEvent(
            action=action, args={"selector": selector, "text": text},
            document_id=core.name, source="core-action"))
        if core._ref is not None:
            core._ref.actions.append({"op": action, "args": {
                k: v for k, v in (("selector", selector), ("text", text))
                if v is not None}})
        loc = core._page.locator(selector or "*").first
        if action == "click":
            await loc.click(timeout=ms)
        elif action == "write":
            await loc.fill(text or "", timeout=ms)
        await drain(core)

    async def _await_for(self, core: Any, selector: str,
                         timeout: float | None) -> None:
        await core._page.wait_for_selector(selector, timeout=(timeout or 30.0) * 1000)
        await drain(core)

    async def _aselect(self, core: Any, selector: str, index: int,
                       error: Any) -> Any:
        from .document_core import DocumentCore
        loc = core._page.locator(selector)
        if await loc.count() <= index:
            sub = DocumentCore(url=core.url, kind="html",
                               status_code=core.status_code)
            sub._client = core._client
            sub._missing = True
            return sub
        html = await loc.nth(index).evaluate("el => el.outerHTML")
        sub = DocumentCore(url=core.url, final_url=core.final_url, kind="html",
                           content=html.encode(), status_code=core.status_code)
        sub._client = core._client
        sub.root = core.name
        nid = selector[1:] if selector.startswith("#") and " " not in selector else None
        sub._events = [e for e in core._events if isinstance(e, DOMUpdateEvent)
                       and (nid is None or nid in e.detail.get("ids", []))]
        return sub

    async def _aselect_all(self, core: Any, selector: str) -> list[Any]:
        from .document_core import DocumentCore
        loc = core._page.locator(selector)
        out: list[Any] = []
        for i in range(await loc.count()):
            html = await loc.nth(i).evaluate("el => el.outerHTML")
            sub = DocumentCore(url=core.url, final_url=core.final_url,
                               kind="html", content=html.encode(),
                               status_code=core.status_code)
            sub._client = core._client
            sub.root = core.name
            out.append(sub)
        return out


__all__ = ["LiveBacking", "INIT_JS", "drain", "console_event"]
