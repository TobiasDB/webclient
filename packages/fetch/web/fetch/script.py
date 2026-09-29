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

from .models import Script

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


#: the default DOM recorder -- a MutationObserver installed after load; its drained mutations
#: become a DOMEvent. Replace with ``Script(name="rrweb", on="load", js=RRWEB_BUNDLE, drain=...)``
#: for full rrweb capture.
DOM_RECORDER = Script(name="dom", on="load", js=_DOM_RECORD_JS, drain=_DOM_DRAIN_JS)


# Force every shadow root OPEN so the snapshot can read it (a closed root is otherwise invisible).
# An INIT script: it runs before the page attaches any shadow root.
_OPEN_SHADOW_JS = (
    "(()=>{const o=Element.prototype.attachShadow;"
    "Element.prototype.attachShadow=function(i){return o.call(this,Object.assign({},i,{mode:'open'}))};})();"
)

# At snapshot time, INLINE the open shadow roots and same-origin iframe/frame documents into the
# light DOM, so the read HTML (and every selector) sees data that lives inside a web component or a
# frame -- the OG client's "deep DOM". Cross-origin frames are skipped (unreadable). Idempotent.
_DEEP_DOM_JS = (
    "(()=>{function inline(r){for(const el of r.querySelectorAll('*')){"
    "if(el.shadowRoot&&!el.__deep){el.__deep=1;const h=document.createElement('shadow-root');"
    "h.innerHTML=el.shadowRoot.innerHTML;el.appendChild(h);inline(h);}}}"
    "inline(document);"
    "for(const f of document.querySelectorAll('iframe,frame')){if(f.__deep)continue;try{"
    "const d=f.contentDocument;if(d&&d.body){f.__deep=1;const h=document.createElement('frame-body');"
    "h.innerHTML=d.body.innerHTML;(f.parentNode||document.body).insertBefore(h,f.nextSibling);}}catch(e){}}})();"
)

#: force shadow roots open (before navigation) so :data:`DEEP_DOM` can read them.
OPEN_SHADOW = Script(name="open_shadow", on="init", js=_OPEN_SHADOW_JS)
#: inline shadow roots + same-origin frames into the light DOM at each snapshot (the deep DOM).
DEEP_DOM = Script(name="deep_dom", on="snapshot", js=_DEEP_DOM_JS)


class ScriptRegistry:
    """A named, mutable collection of page scripts the browser backend installs. Register scripts,
    ``enable`` / ``disable`` them by name; only the enabled ones are installed. This lets a caller
    add rrweb, a custom recorder, or a stealth patch -- and toggle capture -- without reconstructing
    the backend. Enabled state is tracked here (not on the shared Script), so a module-level Script
    is never mutated."""

    def __init__(self, scripts: "tuple[Script, ...]" = (DOM_RECORDER,)) -> None:
        self._scripts: dict[str, Script] = {}
        self._enabled: dict[str, bool] = {}
        for s in scripts:
            self.register(s)

    def register(self, script: Script, *, enabled: bool = True) -> "ScriptRegistry":
        """Add (or replace) a script by name; returns self for chaining."""
        self._scripts[script.name] = script
        self._enabled[script.name] = enabled
        return self

    def enable(self, name: str) -> "ScriptRegistry":
        self._enabled[name] = True
        return self

    def disable(self, name: str) -> "ScriptRegistry":
        self._enabled[name] = False
        return self

    def enabled(self) -> "tuple[Script, ...]":
        """The scripts currently enabled, in registration order -- what the backend installs."""
        return tuple(s for n, s in self._scripts.items() if self._enabled.get(n))


#: the default registry: the DOM recorder + the deep-DOM pair (open shadow roots, inline them and
#: same-origin frames at snapshot), all enabled. Disable ``deep_dom`` / ``open_shadow`` by name for
#: a raw light-DOM capture.
def default_scripts() -> ScriptRegistry:
    return ScriptRegistry((DOM_RECORDER, OPEN_SHADOW, DEEP_DOM))


__all__ = ["DOM_RECORDER", "OPEN_SHADOW", "DEEP_DOM", "ScriptRegistry", "default_scripts"]
