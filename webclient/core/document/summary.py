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
)


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
        return Runtime(
            is_spa=framework is not None or bool(mutations),
            framework=framework,
            uses_xhr=any(e.resource_type == "xhr" for e in xhr),
            uses_fetch=any(e.resource_type == "fetch" for e in xhr),
            xhr_endpoints=[
                XhrCall(
                    method=(
                        str(e.request.method).upper()
                        if e.request is not None
                        else "GET"
                    ),
                    url=str(e.request.dispatch("url")) if e.request is not None else "",
                )
                for e in xhr
            ],
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
        )


class SummaryBacking(Backing):
    """The unifier: ``doc.summary(*include, exclude=...)`` assembles the requested
    facet sections into a :class:`Summary` (default: every applicable facet). A
    facet whose backing does not apply to this document (e.g. ``metadata`` on
    json, ``runtime`` on a static fetch) is simply left ``None``. Supersedes the
    old title/markdown digest -- markdown is reachable via ``render('markdown')``."""

    provides = frozenset({"summary"})
    gate = "summary"

    def applies(self, core: "Document") -> bool:
        return True

    def summary(
        self, core: "Document", *include: str, exclude: Any = ()
    ) -> Summary:
        drop = {exclude} if isinstance(exclude, str) else set(exclude)
        want = (set(include) if include else set(FACETS)) - drop
        data = {f: core.dispatch(f) for f in FACETS if f in want and core.has_op(f)}
        return Summary(**data)


__all__ = [
    "TransportBacking",
    "MetadataBacking",
    "StructureBacking",
    "RuntimeBacking",
    "ProbeBacking",
    "SummaryBacking",
]
