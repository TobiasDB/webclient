"""StructureBacking: the ``structure`` facet -- body shape of an html/xml tree
(a table of contents, counts, forms, links, detected pagination)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from ..web_core import Backing
from .html import _norm, tree
from .models import Form, Structure, TocEntry

if TYPE_CHECKING:
    from . import Document

_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")


class StructureBacking(Backing):
    """The ``structure`` facet: body shape -- toc, counts, forms, links,
    pagination."""

    provides = frozenset({"structure"})
    gate = "ok"

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


__all__ = ["StructureBacking"]
