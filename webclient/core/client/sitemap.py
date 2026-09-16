"""SiteBacking: the client's site-discovery verbs -- ``robots`` (hunt the
``robots.txt``) and ``sitemap`` (hunt the ``sitemap.xml`` page URLs).

Both are cheap, dispatched IO ops (a couple of fetches), built on the interface
(``core.afetch`` + ``core.ref``) like ``search`` -- so remote is a pure dispatch
difference (they ride ``/execute``, no bespoke endpoint). ``sitemap`` reads
``robots`` for ``Sitemap:`` directives, falls back to the well-known
``/sitemap.xml``, then parses each sitemap -- expanding a ``<sitemapindex>`` one
level into its child sitemaps -- and returns the page URLs as
:class:`~webclient.core.reference.Reference` objects (deduped, bounded). To crawl a
sitemap, feed it in: ``wc.crawl(wc.sitemap(url))``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

from ..reference import Reference, from_url
from ..web_core import Backing
from .models import Robots

if TYPE_CHECKING:
    from . import WebClient


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""


def _robots_sitemaps(text: str) -> list[str]:
    """The ``Sitemap:`` directives in a robots.txt (case-insensitive key)."""
    out: list[str] = []
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() == "sitemap" and value.strip():
            out.append(value.strip())
    return out


def _localname(tag: Any) -> str:
    """An lxml tag's local name (namespace stripped); '' for comments/PIs."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _locs(content: bytes) -> "tuple[str, list[str]]":
    """Parse a sitemap document: its root local-name (``sitemapindex`` /
    ``urlset``) and the ``<loc>`` URLs it lists. Malformed XML -> ('', [])."""
    from lxml import etree

    try:
        root = etree.fromstring(content, parser=etree.XMLParser(recover=True))
    except (etree.XMLSyntaxError, ValueError):
        return "", []
    if root is None:
        return "", []
    locs = [
        (el.text or "").strip()
        for el in root.iter()
        if _localname(el.tag) == "loc" and (el.text or "").strip()
    ]
    return _localname(root.tag), locs


class SiteBacking(Backing):
    """The client's site-discovery verbs: ``robots`` + ``sitemap`` (both dispatched)."""

    provides = frozenset({"sitemap", "robots"})
    collections = frozenset({"sitemap"})  # sitemap -> a Collection; robots -> one model
    io = frozenset({"sitemap", "robots"})  # IO ops: the interface bridges them (dispatch)
    gate = "ok"

    async def robots(self, core: "WebClient", url: Any) -> "Robots":
        """Hunt ``url``'s site ``robots.txt`` -- CHEAP (one fetch). Returns a
        :class:`Robots`: its ``Sitemap:`` URLs plus ``allowed(url)`` / ``delay()`` over
        the rules. A site with no robots.txt yields ``exists=False`` (never raises)."""
        origin = _origin(str(getattr(url, "url", url)))
        if not origin:
            return Robots()
        rurl = f"{origin}/robots.txt"
        doc = await core.afetch(core.ref(rurl), optional=True)
        if not doc.ok or not doc.content:
            return Robots(url=rurl, exists=False)
        text = doc.content.decode("utf-8", "replace")
        return Robots(url=rurl, exists=True, content=text, sitemaps=_robots_sitemaps(text))

    async def sitemap(
        self, core: "WebClient", url: Any, *, limit: int = 5000
    ) -> "list[Reference]":
        """Hunt ``url``'s site ``sitemap.xml`` page URLs -- CHEAP (a couple of fetches),
        NOT a crawl. Reads ``robots`` for ``Sitemap:`` directives (else the well-known
        ``/sitemap.xml``), fetches each, and collects the ``<loc>`` page URLs --
        expanding a ``<sitemapindex>`` one level into its child sitemaps. Returns
        deduped References, capped at ``limit``. A site with no sitemap yields an empty
        list (never raises). To crawl these, feed them in: ``wc.crawl(wc.sitemap(url))``."""
        origin = _origin(str(getattr(url, "url", url)))
        if not origin:
            return []
        sources = await self._sitemap_sources(core, origin)
        seen: set[str] = set()
        out: list[Reference] = []
        for src in sources:
            if len(out) >= limit:
                break
            kind, locs = await self._fetch_locs(core, src)
            if kind == "sitemapindex":  # a level of nested sitemaps -> expand once
                for child in locs:
                    if len(out) >= limit:
                        break
                    _, pages = await self._fetch_locs(core, child)
                    self._collect(pages, seen, out, limit)
            else:
                self._collect(locs, seen, out, limit)
        for ref in out:
            ref._client = core
        return out

    async def _sitemap_sources(self, core: "WebClient", origin: str) -> list[str]:
        """The sitemap URLs to read: the ``robots`` op's ``Sitemap:`` directives, else
        the well-known ``/sitemap.xml``."""
        robots = await self.robots(core, origin)
        return robots.sitemaps or [f"{origin}/sitemap.xml"]

    async def _fetch_locs(
        self, core: "WebClient", src: str
    ) -> "tuple[str, list[str]]":
        doc = await core.afetch(core.ref(src), optional=True)
        if not doc.ok or not doc.content:
            return "", []
        kind, locs = _locs(doc.content)
        return kind, [urljoin(src, loc) for loc in locs]

    def _collect(
        self, urls: list[str], seen: set[str], out: list[Reference], limit: int
    ) -> None:
        for u in urls:
            if len(out) >= limit:
                return
            if u not in seen:
                seen.add(u)
                out.append(from_url(u))


__all__ = ["SiteBacking"]
