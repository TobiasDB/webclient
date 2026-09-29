"""Page metadata -- title, description, canonical URL, OpenGraph, JSON-LD, and feed links.

A pure read over a parsed markup :class:`~web.parse.Document`: the head/meta facts a scraper or an
onboarding pipeline wants without hand-writing selectors each time. JSON-LD blocks are parsed
(the structured-data a page publishes about itself); feed links are resolved absolute.
"""

from __future__ import annotations

import json as _json
from typing import TYPE_CHECKING
from urllib.parse import urljoin

from pydantic import BaseModel

from .jsonpath import JSON
from .nodes import raw_text as _raw_text

if TYPE_CHECKING:
    from .document import Document


class Metadata(BaseModel):
    """The head-level facts of a page."""

    title: str | None = None
    description: str | None = None
    canonical: str | None = None
    og: dict[str, str] = {}  # OpenGraph (og:*) properties
    ld_json: list[JSON] = []  # parsed application/ld+json blocks
    feeds: list[str] = []  # RSS/Atom feed URLs (absolute)


def _content(doc: "Document", css: str) -> str | None:
    el = doc.select(css)
    return el.attr("content") if el is not None else None


def metadata(doc: "Document") -> Metadata:
    """Extract :class:`Metadata` from a markup document (empty for non-markup)."""
    if doc.kind not in ("html", "xml"):
        return Metadata()

    og = {
        (p := el.attr("property") or ""): el.attr("content") or ""
        for el in doc.select_all("meta[property^='og:']")
        if el.attr("property")
    }
    title_el = doc.select("title")
    title = (title_el.text if title_el is not None else None) or og.get("og:title")

    ld: list[JSON] = []
    for el in doc.select_all("script[type='application/ld+json']"):
        try:  # raw_text, not el.text -- whitespace collapse would corrupt string values
            ld.append(_json.loads(_raw_text(el._node) or "null"))
        except _json.JSONDecodeError:
            continue  # a malformed block is skipped, not fatal

    canonical_el = doc.select("link[rel=canonical]")
    return Metadata(
        title=title,
        description=_content(doc, "meta[name=description]") or og.get("og:description"),
        canonical=(
            urljoin(doc.url, canonical_el.attr("href"))
            if canonical_el is not None and canonical_el.attr("href")
            else None
        ),
        og=og,
        ld_json=ld,
        feeds=[
            urljoin(doc.url, e.attr("href") or "")
            for e in doc.select_all(
                "link[type='application/rss+xml'], link[type='application/atom+xml']"
            )
            if e.attr("href")
        ],
    )


__all__ = ["Metadata", "metadata"]
