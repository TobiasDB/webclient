"""SignalsBacking: the ``signals`` facet -- self-describing facts about a resolved
response (replaces the old ``runtime`` + ``probe`` facets, which were two shapes of
the same information).

Two families, one surface:

* **access signals** -- ``anti_bot`` / ``blocked`` / ``paywall`` / ``login_wall`` --
  read from the status / headers / cookies / body by the pure
  :func:`webclient.resiliency.detect.classify`; available on ANY fetch (static or
  browser).
* the **JS-nature signal** -- ``spa`` -- read from the captured DOM/network events a
  browser render produced (empty on a static fetch), plus the framework / SPA-shell
  markers visible in the served HTML.

Each op returns a :class:`Signal` (``present`` + ``reason`` + ``value`` + ``remedy``),
so a caller can read exactly the field it wants and the ``auto`` resolve loop can read
``remedy`` to decide what to escalate to. A total facet: it always applies; an
undetected signal is ``Signal(present=False)``, not an error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlparse

from ...models import DOMUpdateEvent, NetworkEvent
from ...resiliency.detect import Signals, classify
from ..web_core import Backing
from .html import _norm
from .models import Signal, XhrCall

if TYPE_CHECKING:
    from . import Document

#: (framework name, a marker substring in the served HTML)
_FRAMEWORKS = (
    ("next", "__NEXT_DATA__"),
    ("next", "/_next/"),
    ("nuxt", "__NUXT__"),
    ("nuxt", "/_nuxt/"),
    ("react", "data-reactroot"),
    ("react", "react-dom"),
    ("angular", "ng-version"),
    ("vue", "data-v-"),
    ("svelte", "svelte-"),
    ("gatsby", "___gatsby"),
    ("remix", "__remixContext"),
    ("astro", "astro-island"),
    ("aem-edge", "window.hlx"),  # Adobe Edge Delivery / Helix / Franklin
    ("aem-edge", "/scripts/aem.js"),
    ("aem-edge", "/scripts/scripts.js"),
)

#: generic client-hydration structural markers -> a Single-Page-App even when no
#: named framework matched (an SPA root container, a serialised initial-state blob,
#: or the block-status attributes Adobe Edge Delivery sets).
_SPA_MARKERS = (
    'id="root"',
    'id="app"',
    'id="__next"',
    'id="__nuxt"',
    "data-server-rendered",
    "__INITIAL_STATE__",
    "__APOLLO_STATE__",
    "__PRELOADED_STATE__",
    "data-block-status",
    "data-section-status",
)

#: SPA thresholds on ``injected_ratio``. ``_SPA_RATIO`` alone judges a page a SPA;
#: the lower ``_SPA_MAIN_RATIO`` does too *when* the injected content landed in the
#: main area AND came from the page's own origin (position + provenance).
_SPA_RATIO = 0.4
_SPA_MAIN_RATIO = 0.15


def _framework(html: str) -> str | None:
    for name, marker in _FRAMEWORKS:
        if marker in html:
            return name
    return None


class SignalsBacking(Backing):
    """The ``signals`` facet: access + JS-nature detection over a resolved document,
    each as a self-describing :class:`Signal`. Total (always applies)."""

    provides = frozenset(
        {"signals", "spa", "body_injected", "xhr_composed", "client_shell",
         "anti_bot", "blocked", "paywall", "login_wall", "framework", "xhr_endpoints"}
    )
    gate = "ok"

    def applies(self, core: "Document") -> bool:
        return True

    # -- the whole set (the LLM-facing digest) --------------------------------
    def signals(self, core: "Document") -> "list[Signal]":
        """Every granular signal that fired, most-actionable first -- the compact
        digest of "what is notable about this response". Several may point at the same
        conclusion (both ``body_injected`` and ``xhr_composed`` on one SPA). Empty when
        nothing was detected. (The ``spa`` roll-up is excluded -- it is those same
        granular signals, or'd.)"""
        ordered = [
            self.anti_bot(core), self.blocked(core), self.login_wall(core),
            self.paywall(core), self.xhr_composed(core), self.body_injected(core),
            self.client_shell(core),
        ]
        return [s for s in ordered if s.present]

    def _classify(self, core: "Document") -> Signals:
        return classify(core.status_code, core.response_headers, core._set_cookies, core.content)

    # -- access signals (any fetch: from status / headers / cookies / body) ---
    def anti_bot(self, core: "Document") -> Signal:
        s = self._classify(core)
        if not s.anti_bot:
            return Signal(name="anti_bot")
        named = s.anti_bot != "challenge"
        return Signal(
            name="anti_bot",
            present=True,
            reason=(f"{s.anti_bot} anti-bot challenge" if named
                    else f"a bot-challenge status ({core.status_code}) with no named vendor"),
            value=s.anti_bot,
            remedy="stealth" if named else "proxy",
        )

    def blocked(self, core: "Document") -> Signal:
        s = self._classify(core)
        if not s.blocked:
            return Signal(name="blocked")
        return Signal(
            name="blocked",
            present=True,
            reason=f"a hard block status ({core.status_code})",
            value=core.status_code,
            remedy="proxy",
        )

    def paywall(self, core: "Document") -> Signal:
        if not self._classify(core).paywall:
            return Signal(name="paywall")
        return Signal(
            name="paywall",
            present=True,
            reason="a paywall (402 or isAccessibleForFree:false)",
            remedy=None,  # nothing the transport ladder can do
        )

    def login_wall(self, core: "Document") -> Signal:
        if not self._classify(core).login_wall:
            return Signal(name="login_wall")
        return Signal(
            name="login_wall",
            present=True,
            reason=("a 401 auth wall" if core.status_code == 401
                    else "a login form with little other content"),
            remedy=None,  # needs credentials, not an escalation
        )

    # -- SPA family: independent detectors, several may fire for one page ------
    def body_injected(self, core: "Document") -> Signal:
        """The share of the page's body text that appeared AFTER the initial response
        (net growth past the DOMContentLoaded baseline). A high ratio means the
        content is client-rendered -- a browser would recover it. Browser-only
        evidence; ``value`` is the ratio (0.0 on a static fetch)."""
        ratio, _, _ = self._injection(core)
        if ratio < _SPA_RATIO:
            return Signal(name="body_injected", value=ratio)
        return Signal(
            name="body_injected", present=True, value=ratio, remedy="browser",
            reason=f"{ratio:.0%} of the page's text was injected after the initial response",
        )

    def xhr_composed(self, core: "Document") -> Signal:
        """Main-area content correlated with the page's OWN-origin XHR/fetch calls
        (DOM mutation x network, rrweb-style): the page composed its main content
        client-side from data APIs. ``value`` lists those endpoints -- an agent can
        often skip the render and fetch them directly."""
        ratio, in_main, same_origin_xhr = self._injection(core)
        if not (in_main and same_origin_xhr >= 1 and ratio >= _SPA_MAIN_RATIO):
            return Signal(name="xhr_composed")
        eps = [c.url for c in self.xhr_endpoints(core)]
        return Signal(
            name="xhr_composed", present=True, value=eps, remedy="browser",
            reason=f"main content composed from {same_origin_xhr} same-origin XHR call(s) "
                   f"({ratio:.0%} of text injected)",
        )

    def client_shell(self, core: "Document") -> Signal:
        """A client-render shell visible in the SERVED HTML -- a known framework
        marker or an empty hydration root that a bundle populates. Static-observable
        (no render needed), so ``auto`` can decide to escalate from the first fetch."""
        html = (core.content or b"").decode(core.encoding or "utf-8", "replace")
        framework = _framework(html)
        shell = any(m in html for m in _SPA_MARKERS)
        js_required = self._classify(core).js_required  # the SPA-shell regex (both quote styles)
        if not (framework or shell or js_required):
            return Signal(name="client_shell")
        return Signal(
            name="client_shell", present=True, value=framework, remedy="browser",
            reason=(f"a {framework} client-render marker" if framework
                    else "an empty hydration shell that a bundle populates"),
        )

    def spa(self, core: "Document") -> Signal:
        """Roll-up: is the page a Single-Page-App? Present if ANY SPA-family detector
        (``body_injected`` / ``xhr_composed`` / ``client_shell``) fires -- read those
        for the specific evidence. ``value`` names the detectors that fired."""
        fired = [s for s in (self.xhr_composed(core), self.body_injected(core),
                             self.client_shell(core)) if s.present]
        if not fired:
            return Signal(name="spa")
        return Signal(
            name="spa", present=True, remedy="browser",
            value=[s.name for s in fired],
            reason="; ".join(s.reason for s in fired),
        )

    def framework(self, core: "Document") -> "str | None":
        """The detected JS framework name (from markers in the served HTML), or None."""
        html = (core.content or b"").decode(core.encoding or "utf-8", "replace")
        return _framework(html)

    def xhr_endpoints(self, core: "Document") -> "list[XhrCall]":
        """The XHR/fetch calls a browser render captured -- an SPA's real data
        sources, which an agent can often fetch directly instead of rendering."""
        return [
            XhrCall(
                method=str(e.request.method).upper() if e.request is not None else "GET",
                url=str(e.request.dispatch("url")) if e.request is not None else "",
            )
            for e in core._events
            if isinstance(e, NetworkEvent) and e.resource_type in ("xhr", "fetch")
        ]

    # -- injection metrics (browser-only; 0 on a static fetch) ----------------
    def _injection(self, core: "Document") -> "tuple[float, bool, int]":
        """``(injected_ratio, injected_in_main, same_origin_xhr_count)`` from captured
        events. ``injected_ratio`` = NET text grown past the DOMContentLoaded baseline
        / final text (so mere DOM re-org doesn't count). All zero on a static fetch."""
        mutations = [e for e in core._events if isinstance(e, DOMUpdateEvent)]
        xhr = [
            e for e in core._events
            if isinstance(e, NetworkEvent) and e.resource_type in ("xhr", "fetch")
        ]
        page_host = (urlparse(core.final_url or core.url).hostname or "").lower()
        same_origin_xhr = sum(
            1 for e in xhr
            if e.request is not None
            and (urlparse(str(e.request.dispatch("url"))).hostname or "").lower() == page_host
        )
        load_added = [
            e for e in mutations
            if e.kind == "added" and (e.detail or {}).get("phase") == "load"
        ]
        injected_in_main = any((e.detail or {}).get("inMain") for e in load_added)
        stats = core._render_stats or {}
        total_text = int(stats.get("text", 0)) or len(
            _norm((core.content or b"").decode("utf-8", "replace"))
        )
        if stats.get("dclText") is not None and total_text:
            net = max(0, total_text - int(stats["dclText"]))
            ratio = round(net / total_text, 3)
        else:  # no DOMContentLoaded baseline (static fetch / seeded events)
            ratio = 0.0
        return ratio, injected_in_main, same_origin_xhr


__all__ = ["SignalsBacking"]
