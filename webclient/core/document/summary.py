"""Summary facet backings: each reads one aspect of a resolved document into a
section of :class:`webclient.summary.Summary`. Pure projections -- they read what
the resolution already captured, never fetch or escalate. ``transport`` applies
to any document; ``metadata`` / ``structure`` need an html/xml tree.
"""

from __future__ import annotations

import json as _json
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

from ...models import DOMUpdateEvent, NetworkEvent
from ..web_core import Backing
from .models import (
    FACETS,
    Form,
    Metadata,
    Probe,
    Runtime,
    Structure,
    Summary,
    TocEntry,
    Transport,
    XhrCall,
)
from .html import _norm, tree

if TYPE_CHECKING:
    from . import Document

_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")

#: typed Summary fields that a requested include-name maps to directly (rather than
#: the open ``extra`` dict) -- e.g. ``summary(url, "skeleton")`` sets ``.skeleton``.
_DIRECT_FIELDS = set(Summary.model_fields) - set(FACETS) - {"extra"}

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
#: named framework matched (an SPA root container the JS mounts into, a serialised
#: initial-state blob, or the block-status attributes Adobe Edge Delivery sets).
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

#: how many *same-origin* content XHR/fetch requests a render must make before the
#: page is judged client-composed (a Single-Page-App). A server-rendered page ships
#: its content in the HTML and fetches at most a beacon or two; a client-composed
#: one (React/Next hydration, Adobe Edge Delivery blocks, …) fetches its own
#: fragments/data from its own origin -- e.g. news.adobe.com pulls ~9. Third-party
#: analytics/ad calls don't count (they are cross-origin), so this rarely mislabels
#: a plain SSR page.
_SPA_XHR_MIN = 2


def _cdn(h: dict[str, str]) -> str | None:
    """A best-effort CDN name from response headers (lowercased keys/values)."""
    via, server = h.get("via", ""), h.get("server", "")
    if "cf-ray" in h or "cloudflare" in server:
        return "cloudflare"
    if "x-amz-cf-id" in h or "cloudfront" in via:
        return "cloudfront"
    if "x-served-by" in h or "fastly" in server or "fastly" in via:
        return "fastly"
    if "x-akamai-transformed" in h or "akamai" in server:
        return "akamai"
    if "x-vercel-id" in h:
        return "vercel"
    return None


class TransportBacking(Backing):
    """The ``transport`` facet: transport facts from the resolved response
    (values only for the few that are the summary; headers/cookies as keys)."""

    provides = frozenset({"transport"})
    gate = "summary"

    def applies(self, core: "Document") -> bool:
        return True

    def transport(self, core: "Document") -> Transport:
        h = {k.lower(): v for k, v in core.response_headers.items()}
        final = core.final_url or core.url
        return Transport(
            final_url=final,
            status_code=core.status_code,
            ok=core.ok,
            kind=core.kind,
            redirect_chain=(
                [core.url] if core.final_url and core.final_url != core.url else []
            ),
            duration_ms=round(core.elapsed * 1000, 1) if core.elapsed else None,
            content_type=h.get("content-type"),
            encoding=core.encoding,
            size_bytes=len(core.content) or None,
            header_keys=sorted(core.response_headers),
            set_cookie_keys=sorted(core._set_cookies),
            server=h.get("server"),
            cdn=_cdn(h),
            region=h.get("cf-ipcountry") or h.get("x-country"),
        )


def _ld_types(root: object) -> list[str]:
    """The ``@type`` values across all JSON-LD blocks (deduped, order-kept)."""
    types: list[str] = []
    for node in root.cssselect('script[type="application/ld+json"]'):  # type: ignore[attr-defined]
        try:
            data = _json.loads(node.text or "")
        except (ValueError, TypeError):
            continue
        for obj in data if isinstance(data, list) else [data]:
            t = obj.get("@type") if isinstance(obj, dict) else None
            for name in t if isinstance(t, list) else [t]:
                if isinstance(name, str) and name not in types:
                    types.append(name)
    return types


class MetadataBacking(Backing):
    """The ``metadata`` facet: head + schema. Title/description are values; og and
    JSON-LD are reported as key/type names."""

    provides = frozenset({"metadata"})
    gate = "summary"

    def applies(self, core: "Document") -> bool:
        return core.kind in ("html", "xml")

    def metadata(self, core: "Document") -> Metadata:
        root = tree(core)

        def meta(**attr: str) -> str | None:
            sel = "meta" + "".join(f'[{k}="{v}"]' for k, v in attr.items())
            nodes = root.cssselect(sel)
            return nodes[0].get("content") if nodes else None

        def one(sel: str, attr: str) -> str | None:
            nodes = root.cssselect(sel)
            return nodes[0].get(attr) if nodes else None

        schema_types = _ld_types(root)
        canonical = one('link[rel="canonical"]', "href")
        return Metadata(
            title=core.dispatch("title") if core.has_op("title") else None,
            description=meta(name="description") or meta(property="og:description"),
            lang=root.get("lang"),
            canonical_url=(
                urljoin(core.final_url or core.url, canonical) if canonical else None
            ),
            schema_types=schema_types,
            og_keys=sorted(
                {
                    p
                    for el in root.cssselect('meta[property^="og:"]')
                    if (p := el.get("property"))
                }
            ),
            page_type=(schema_types[0] if schema_types else meta(property="og:type")),
            feeds=[
                urljoin(core.final_url or core.url, href)
                for el in root.cssselect(
                    'link[type="application/rss+xml"], link[type="application/atom+xml"]'
                )
                if (href := el.get("href"))
            ],
        )


class StructureBacking(Backing):
    """The ``structure`` facet: body shape -- toc, counts, forms, links,
    pagination."""

    provides = frozenset({"structure"})
    gate = "summary"

    def applies(self, core: "Document") -> bool:
        return core.kind in ("html", "xml")

    def structure(self, core: "Document") -> Structure:
        root = tree(core)
        base = core.final_url or core.url
        host = urlparse(base).hostname or ""

        toc = [
            TocEntry(level=int(el.tag[1]), text=_norm("".join(el.itertext())))
            for el in root.cssselect(",".join(_HEADINGS))
            if _norm("".join(el.itertext()))
        ]
        words = len("".join(root.itertext()).split())
        hrefs = [
            urljoin(base, h)
            for el in root.cssselect("a[href]")
            if (h := el.get("href")) and not h.startswith(("#", "javascript:"))
        ]
        internal = [u for u in hrefs if (urlparse(u).hostname or host) == host]
        forms = [
            Form(
                method=(el.get("method") or "get").lower(),
                action=urljoin(base, el.get("action")) if el.get("action") else None,
                field_names=sorted(
                    {
                        n
                        for f in el.cssselect("input, select, textarea")
                        if (n := f.get("name"))
                    }
                ),
            )
            for el in root.cssselect("form")
        ]
        paginated = bool(
            root.cssselect('a[rel="next"], .next, .pagination, [class*="pagination"]')
        )
        return Structure(
            toc=toc,
            word_count=words or None,
            reading_time_min=max(1, round(words / 200)) if words else None,
            main_content_present=bool(
                root.cssselect("main, article, [role=main], #content, #main")
            ),
            links_internal=len(internal),
            links_external=len(hrefs) - len(internal),
            link_sample=hrefs[:10],
            forms=forms,
            pagination="next-link" if paginated else None,
            media_img=len(root.cssselect("img")),
            media_video=len(root.cssselect("video, iframe")),
        )


def _framework(html: str) -> str | None:
    for name, marker in _FRAMEWORKS:
        if marker in html:
            return name
    return None


class RuntimeBacking(Backing):
    """The ``runtime`` facet: browser-only signals read from captured DOM/network
    events (applies only to a browser-rendered document; ``None`` on a static
    fetch). ``xhr_endpoints`` come from the page's XHR/fetch requests, ``is_spa`` /
    ``framework`` from framework markers + hydration, ``dynamic_elements`` from the
    DOM mutations captured after interaction."""

    provides = frozenset({"runtime"})
    gate = "summary"

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
        is_spa = (
            framework is not None  # a known JS framework / Edge-Delivery marker
            or bool(mutations)  # DOM changed after the initial render
            or any(m in html for m in _SPA_MARKERS)  # a hydration-root / state blob
            or same_origin_xhr >= _SPA_XHR_MIN  # composes itself from its own origin
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
        )


class ProbeBacking(Backing):
    """The ``probe`` facet: what the resolution had to escalate to (browser / proxy
    / anti-bot / a paywall or login wall it hit), projected from the ``ProbeRecord``
    the transport ladder wrote onto the document. Only applies once a probe was
    recorded -- a plain static fetch leaves the facet ``None``."""

    provides = frozenset({"probe"})
    gate = "probe"

    def applies(self, core: "Document") -> bool:
        return core._probe is not None

    def probe(self, core: "Document") -> Probe:
        r = core._probe
        # keep it lean: report only the flags that are true (None otherwise).
        return Probe(
            was_browser_required=r.was_browser_required or None,
            was_proxy_required=r.was_proxy_required or None,
            anti_bot=r.anti_bot,
            js_required=r.js_required or None,
            paywall=r.paywall or None,
            login_wall=r.login_wall or None,
            render_blocked=r.render_blocked or None,
            render_gain=r.render_gain,
        )


class SummaryBacking(Backing):
    """The unifier: ``doc.summary(*include, exclude=...)`` assembles the requested
    facet sections into a :class:`Summary` (default: every applicable facet). A
    facet whose backing does not apply to this document (e.g. ``metadata`` on
    json, ``runtime`` on a static fetch) is simply left ``None``. Supersedes the
    old title/markdown digest -- markdown is reachable via ``render('markdown')``.

    ``include`` names may reach beyond the fixed facets: any *other* argument-free
    backing op the document has (e.g. ``title``, ``text_content``) is called and
    its result placed under ``Summary.extra[name]``. That is how a crawl decides
    exactly which backings populate each page's summary (directive: crawl chooses
    the backings; summary is the open mechanism)."""

    provides = frozenset({"summary"})
    gate = "summary"

    def applies(self, core: "Document") -> bool:
        return True

    def summary(
        self, core: "Document", *include: str, exclude: Any = ()
    ) -> Summary:
        drop = {exclude} if isinstance(exclude, str) else set(exclude)
        # a requested name must be a known facet or an actual backing op -- a typo
        # like summary("structrue") is an error, not a silently-dropped section.
        for name in include:
            if name not in FACETS and not core.has_op(name):
                raise LookupError(
                    f"unknown summary facet/op {name!r}; facets are {FACETS}"
                )
        want = (set(include) if include else set(FACETS)) - drop
        data: dict[str, Any] = {
            f: core.dispatch(f) for f in FACETS if f in want and core.has_op(f)
        }
        # a requested non-facet name that is a typed Summary field (e.g. "skeleton")
        # populates that field; any other backing op goes to the open `extra` section.
        extra: dict[str, Any] = {}
        for name in include:
            if name in FACETS or name in drop or not core.has_op(name):
                continue
            if name in _DIRECT_FIELDS:
                data[name] = core.dispatch(name)
            else:
                extra[name] = core.dispatch(name)
        if extra:
            data["extra"] = extra
        return Summary(**data)


__all__ = [
    "TransportBacking",
    "MetadataBacking",
    "StructureBacking",
    "RuntimeBacking",
    "ProbeBacking",
    "SummaryBacking",
]
