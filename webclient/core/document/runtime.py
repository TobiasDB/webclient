"""RuntimeBacking: the ``runtime`` facet -- browser-only signals read from the
DOM/network events a render captured (empty on a static fetch).

``is_spa`` is GRADED, not greedy: a framework marker, a substantial
``injected_ratio`` (the fraction of the page's text built after load -- net growth
over the DOMContentLoaded baseline, so DOM re-org doesn't count), a hydration
marker, or ``content_from_xhr`` (substantial MAIN-area content from the page's own
XHR data). All the finer metrics are returned for the caller to threshold itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlparse

from ...models import DOMUpdateEvent, NetworkEvent
from ..web_core import Backing
from .html import _norm
from .models import Runtime, XhrCall

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


class RuntimeBacking(Backing):
    """The ``runtime`` facet: browser-only signals read from captured DOM/network
    events (applies only to a browser-rendered document)."""

    provides = frozenset({"runtime"})
    gate = "ok"

    def applies(self, core: "Document") -> bool:
        if core.kind not in ("html", "xml"):
            return False
        if core._page is not None:
            return True
        # a browser render captured DOM mutations or XHR/fetch sub-requests; a
        # static fetch's transport NavigationEvent (resource_type None) does not
        # count -- runtime is a browser-only facet.
        return any(
            isinstance(e, DOMUpdateEvent)
            or (isinstance(e, NetworkEvent) and e.resource_type in ("xhr", "fetch"))
            for e in core._events
        )

    def runtime(self, core: "Document") -> Runtime:
        xhr = [
            e
            for e in core._events
            if isinstance(e, NetworkEvent) and e.resource_type in ("xhr", "fetch")
        ]
        mutations = [e for e in core._events if isinstance(e, DOMUpdateEvent)]
        html = (core.content or b"").decode(core.encoding or "utf-8", "replace")
        framework = _framework(html)
        endpoints = [
            XhrCall(
                method=(
                    str(e.request.method).upper() if e.request is not None else "GET"
                ),
                url=str(e.request.dispatch("url")) if e.request is not None else "",
            )
            for e in xhr
        ]
        # client-composition signal: how many of the page's XHR/fetch calls hit its
        # OWN origin (fetching content/fragments, as an SPA does) vs third-party
        # analytics/ads (which don't imply an SPA).
        page_host = (urlparse(core.final_url or core.url).hostname or "").lower()
        same_origin_xhr = sum(
            1 for c in endpoints if (urlparse(c.url).hostname or "").lower() == page_host
        )
        # HOW MUCH content the page built after navigation, and WHERE.
        load_added = [
            e for e in mutations
            if e.kind == "added" and (e.detail or {}).get("phase") == "load"
        ]
        injected_nodes = sum(int((e.detail or {}).get("added", 0)) for e in load_added)
        injected_in_main = any((e.detail or {}).get("inMain") for e in load_added)
        # injection = NET text grown past the DOMContentLoaded baseline, so merely
        # re-organising existing DOM (re-adds nodes but no new text) doesn't count as
        # client rendering. ratio = net-new text / final text (0 = SSR, ~1 = a shell).
        stats = core._render_stats or {}
        total_text = int(stats.get("text", 0)) or len(
            _norm((core.content or b"").decode("utf-8", "replace"))
        )
        if stats.get("dclText") is not None and total_text:
            net_injected = max(0, total_text - int(stats["dclText"]))
            injected_ratio = round(net_injected / total_text, 3)
        else:  # no DOMContentLoaded baseline (static fetch / seeded events)
            injected_ratio = 0.0
        content_from_xhr = (
            injected_in_main and same_origin_xhr >= 1 and injected_ratio >= _SPA_MAIN_RATIO
        )
        is_spa = (
            framework is not None  # a known JS framework / Edge-Delivery marker
            or injected_ratio >= _SPA_RATIO  # most of the content was built client-side
            or content_from_xhr  # substantial main-area content from its own XHR data
            or any(m in html for m in _SPA_MARKERS)  # a hydration-root / state blob
        )
        return Runtime(
            is_spa=is_spa,
            framework=framework,
            uses_xhr=any(e.resource_type == "xhr" for e in xhr),
            uses_fetch=any(e.resource_type == "fetch" for e in xhr),
            xhr_endpoints=endpoints,
            dynamic_elements=sorted(
                {f"{e.kind}:{e.selector}" if e.selector else e.kind for e in mutations}
            ),
            injected_ratio=injected_ratio,
            injected_nodes=injected_nodes,
            injected_in_main=injected_in_main,
            content_from_xhr=content_from_xhr,
        )


__all__ = ["RuntimeBacking"]
