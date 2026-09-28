"""``Script`` -- a page script the browser transport injects, plus the built-in DOM recorder.

A Script is JS run in the page at a lifecycle stage (``init`` before navigation, ``load`` after),
optionally with a ``drain`` expression the fetcher evaluates to pull buffered output back out. DOM
capture is exactly this: ``Script(on="load", js=<recorder>, drain=<drain>)`` installs a recorder
that buffers DOM changes; the fetcher drains it and converts the output into a
:class:`~web.fetch.events.DOMEvent`. rrweb is the same shape -- swap its bundle in as ``js`` -- so
the mechanism does not change, only the recorder.

The built-in :data:`DOM_RECORDER` is a small MutationObserver (no 140 KB bundle): enough to
record what actions change for replay, and a clean default. Provide your own Script (rrweb) for
richer capture.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

_DOM_RECORD_JS = """
(() => {
  if (window.__wc_dom) return;
  window.__wc_dom = [];
  new MutationObserver((muts) => {
    for (const m of muts) window.__wc_dom.push({
      type: m.type, target: (m.target.nodeName || '').toLowerCase(),
      added: m.addedNodes.length, removed: m.removedNodes.length,
      attr: m.attributeName || null, t: Date.now(),
    });
  }).observe(document.documentElement, {childList: true, subtree: true, attributes: true, characterData: true});
})();
"""

_DOM_DRAIN_JS = "() => { const e = window.__wc_dom || []; window.__wc_dom = []; return e; }"


class Script(BaseModel):
    """A page script. ``on`` picks the lifecycle stage; ``drain`` (optional) is a JS expression
    the fetcher evaluates after the page settles to pull buffered output into an event."""

    name: str
    js: str
    on: Literal["init", "load"] = "load"
    drain: str = ""


#: the default DOM recorder -- a MutationObserver installed after load; its drained mutations
#: become a DOMEvent. Replace with ``Script(name="rrweb", on="load", js=RRWEB_BUNDLE, drain=...)``
#: for full rrweb capture.
DOM_RECORDER = Script(name="dom", on="load", js=_DOM_RECORD_JS, drain=_DOM_DRAIN_JS)


__all__ = ["Script", "DOM_RECORDER"]
