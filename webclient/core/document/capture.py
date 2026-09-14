"""Browser capture primitives -- the injected JS and the helpers that turn raw
page signals (console lines, sub-requests, DOM mutations) into events on a
document.

Neutral ground: the *live backing* uses these (its ``dom_mutations`` reads what
``drain`` records, its ``page_scripts`` install ``INIT_JS``) and so does the
*client* (``_alive`` wraps the load-time console/network into events, and drains
after replay). Keeping them here -- not in the backing -- means the client never
reaches up into a backing module.
"""

from __future__ import annotations

from typing import Any, cast

from ...models import ConsoleEvent, DOMUpdateEvent, NetworkEvent

#: installed on every navigation -- an id-path-tagging MutationObserver feeding
#: ``window.__wc_mutations`` (declared by ``LiveBacking.page_scripts``).
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

#: read + clear the mutation buffer.
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


__all__ = ["INIT_JS", "DRAIN_JS", "drain", "console_event", "network_event"]
