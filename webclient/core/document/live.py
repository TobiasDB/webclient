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
    from . import Document

#: installed on every navigation -- an id-path-tagging MutationObserver feeding
#: ``window.__wc_mutations`` (see ``LiveBacking.page_scripts``).
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

#: read + clear the mutation buffer (run after replay to discard load noise).
DRAIN_JS = "() => { const m = window.__wc_mutations || []; window.__wc_mutations = []; return m; }"

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


async def drain(doc: Any) -> None:
    """Move any pending DOM mutations off the page onto the document. A short
    settle lets the observer's microtask deliver records from the last action."""
    await doc._page.wait_for_timeout(30)
    for r in await doc._page.evaluate(DRAIN_JS):
        doc._events.append(
            DOMUpdateEvent(
                kind=cast(Any, _kind(r)), detail={"ids": r["ids"]}, document_id=doc.name
            )
        )


def console_event(level: str, text: str, doc: Any) -> ConsoleEvent:
    return ConsoleEvent(
        level=cast(Any, _LEVELS.get(level, "log")), text=text, document_id=doc.name
    )


def network_event(method: str, url: str, resource_type: str, doc: Any) -> NetworkEvent:
    """A browser sub-request captured onto the document (an XHR/fetch the page
    made) -- the raw material for the summary ``runtime`` facet's xhr_endpoints."""
    from ..reference import from_url

    return NetworkEvent(
        request=from_url(url, cast(Any, method.lower())),
        resource_type=resource_type,
        document_id=doc.name,
    )


class LiveBacking(Backing):
    """Interaction + live selection on a browser page (capability ``page``)."""

    provides = frozenset(
        {"click", "write", "wait_for", "select", "select_all", "evaluate", "screenshot"}
    )
    props = frozenset({"dom_mutations", "console"})
    #: the always-IO browser interactions -> awaitable under async. ``select`` /
    #: ``select_all`` are omitted: on a *static* document (the common case) they
    #: are in-memory (HtmlBacking), so the surface types them synchronously.
    io = frozenset({"click", "write", "wait_for", "evaluate", "screenshot"})
    #: the browser scripts this backing owns: the mutation observer (``init``,
    #: read by ``dom_mutations`` via ``drain``) and the buffer drain (``drain``
    #: phase, run after replay to discard load-time mutations). The client gathers
    #: and installs them; the backing owns the *what*.
    page_scripts = (PageScript(INIT_JS, "init"), PageScript(DRAIN_JS, "drain"))
    gate = "page"

    def applies(self, core: Any) -> bool:
        return core._page is not None

    def on_load(self, core: Any, result: Any) -> None:
        """Wrap the load-time console/network the client captured (a
        ``clients.PageResult``) into events on the document -- the client hands
        back raw facts and fires this; the backing owns the shaping."""
        for level, text in result.console:
            core._events.append(console_event(level, text, core))
        for method, url, rtype in result.network:  # XHR/fetch the page issued
            if rtype in ("xhr", "fetch"):
                core._events.append(network_event(method, url, rtype, core))

    def _loop(self, core: Any) -> Any:
        return core._client.loop()

    # -- captured event views ------------------------------------------------
    def dom_mutations(self, core: Any) -> "list[DOMUpdateEvent]":
        return [e for e in core._events if isinstance(e, DOMUpdateEvent)]

    def console(self, core: Any) -> "list[ConsoleEvent]":
        return [e for e in core._events if isinstance(e, ConsoleEvent)]

    # -- interactions (IO: async def; the interface bridges via dispatch) -----
    async def click(
        self,
        core: Any,
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
        core: Any,
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
        core: Any,
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
            if optional and "Timeout" in type(exc).__name__:
                return core
            if "Timeout" in type(exc).__name__:
                raise select_error(f"wait_for: no {selector!r} within timeout") from exc
            raise
        return core

    # ``select`` / ``select_all`` are NOT ``io``: on a static document (the common
    # case) they are in-memory (HtmlBacking). On a live page they bridge on the
    # engine loop here (a live+async selection is an untested edge).
    def select(
        self,
        core: Any,
        selector: str,
        *,
        index: int = 0,
        optional: bool = False,
        error: Any = None,
    ) -> "Document":
        from ...errors import RETURN

        return cast(
            "Document",
            self._loop(core).run(
                self._aselect(core, selector, index, RETURN if optional else error)
            ),
        )

    def select_all(self, core: Any, selector: str) -> "list[Document]":
        return cast(
            "list[Document]",
            self._loop(core).run(self._aselect_all(core, selector)),
        )

    async def evaluate(self, core: Any, script: str) -> Any:
        return await core._page.evaluate(script)

    async def screenshot(self, core: Any, selector: str | None = None) -> "Document":
        return await self._ashot(core, selector)

    # -- async bodies --------------------------------------------------------
    async def _aact(
        self,
        core: Any,
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
            if optional and "Timeout" in type(exc).__name__:
                return
            if "Timeout" in type(exc).__name__:
                from ...errors import select_error

                raise select_error(f"{action}: no target for {selector!r}") from exc
            raise
        await drain(core)

    async def _await_for(
        self, core: Any, selector: str | None, timeout: float | None
    ) -> None:
        if selector is not None:
            await core._page.wait_for_selector(
                selector, timeout=(timeout or 30.0) * 1000
            )
        elif timeout is not None:
            await core._page.wait_for_timeout(timeout * 1000)
        await drain(core)

    async def _ashot(self, core: Any, selector: str | None) -> Any:
        from . import Document

        target = core._page if selector is None else core._page.locator(selector).first
        data = await target.screenshot(type="png")
        shot = Document(
            url=core.url, kind="binary", content=data, status_code=core.status_code
        )
        shot._client = core._client
        return shot

    async def _aselect(self, core: Any, selector: str, index: int, error: Any) -> Any:
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
            and (nid is None or nid in e.detail.get("ids", []))
        ]
        return sub

    async def _aselect_all(self, core: Any, selector: str) -> list[Any]:
        from . import Document

        loc = core._page.locator(selector)
        out: list[Any] = []
        for i in range(await loc.count()):
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
