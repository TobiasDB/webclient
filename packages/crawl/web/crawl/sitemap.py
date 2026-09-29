"""Sitemaps -- a site's own list of its URLs, a far better seed set than link-walking from a page.

Handles both a URL set (``<urlset><url><loc>``) and a sitemap INDEX (``<sitemapindex><sitemap>
<loc>``, pointing at more sitemaps), recursing into an index up to a bound. Large sites commonly
serve GZIPPED sitemap files (``sitemap.xml.gz`` as ``application/gzip`` -- not HTTP
Content-Encoding, so the transport does not decompress them); those are decompressed here before
parsing. Parsing is over the parse layer's XML tree; fetching is over the Resolver.
"""

from __future__ import annotations

import gzip
from urllib.parse import urljoin, urlsplit

from web.fetch import Request
from web.parse import Document, parse
from web.resolve import Resolver


def _locs(doc: Document) -> list[str]:
    return [el.text for el in doc.select_all("loc") if el.text]


def _ungzip(doc: Document, url: str) -> Document:
    """Re-parse a gzipped sitemap file (magic ``1f 8b``) as XML; a non-gzip doc passes through."""
    if doc.content[:2] != b"\x1f\x8b":
        return doc
    try:
        return parse(
            gzip.decompress(doc.content), content_type="application/xml", url=url
        )
    except (OSError, EOFError):  # truncated/invalid gzip -> leave as-is
        return doc


async def sitemap_urls(
    resolver: Resolver, source: str, *, max_maps: int = 20
) -> list[str]:
    """Every page URL advertised by the sitemap at ``source`` -- following a sitemap index into its
    child sitemaps (up to ``max_maps`` fetched in all). ``source`` may be a full sitemap URL or a
    site base (then ``/sitemap.xml`` is assumed)."""
    parts = urlsplit(source)
    start = (
        source
        if parts.path not in ("", "/")
        else urljoin(f"{parts.scheme}://{parts.netloc}", "/sitemap.xml")
    )
    out: list[str] = []
    queue = [start]
    seen: set[str] = set()
    fetched = 0
    while queue and fetched < max_maps:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        doc = _ungzip(await resolver.resolve(Request(url=url)), url)
        fetched += 1
        if (
            doc.select("sitemapindex") is not None
        ):  # an index: its <loc>s are child sitemaps
            queue.extend(loc for loc in _locs(doc) if loc not in seen)
        else:  # a urlset: its <loc>s are pages
            out.extend(_locs(doc))
    return out


__all__ = ["sitemap_urls"]
