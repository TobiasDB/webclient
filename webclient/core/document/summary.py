"""Summary facet backings: each reads one aspect of a resolved document into a
section of :class:`webclient.summary.Summary`. Pure projections -- they read what
the resolution already captured, never fetch or escalate. ``transport`` applies
to any document; ``metadata`` / ``structure`` need an html/xml tree.
"""

from __future__ import annotations

import json as _json
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from ...summary import Form, Metadata, Structure, TocEntry, Transport
from ..web_core import Backing
from .html import _norm, tree

if TYPE_CHECKING:
    from . import DocumentCore

_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")


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

    def applies(self, core: "DocumentCore") -> bool:
        return True

    def transport(self, core: "DocumentCore") -> Transport:
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

    def applies(self, core: "DocumentCore") -> bool:
        return core.kind in ("html", "xml")

    def metadata(self, core: "DocumentCore") -> Metadata:
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

    def applies(self, core: "DocumentCore") -> bool:
        return core.kind in ("html", "xml")

    def structure(self, core: "DocumentCore") -> Structure:
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


__all__ = ["TransportBacking", "MetadataBacking", "StructureBacking"]
