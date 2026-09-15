"""MetadataBacking: the ``metadata`` facet -- head + schema of an html/xml tree.
Title/description are values; og and JSON-LD are reported as key/type names."""

from __future__ import annotations

import json as _json
from typing import TYPE_CHECKING
from urllib.parse import urljoin

from ..web_core import Backing
from .html import tree
from .models import Metadata

if TYPE_CHECKING:
    from . import Document


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
    gate = "ok"

    def applies(self, core: "Document") -> bool:
        return core.kind in ("html", "xml")

    def metadata(self, core: "Document") -> Metadata:
        root = tree(core)
        base = core.final_url or core.url

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
            canonical_url=urljoin(base, canonical) if canonical else None,
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
                urljoin(base, href)
                for el in root.cssselect(
                    'link[type="application/rss+xml"], link[type="application/atom+xml"]'
                )
                if (href := el.get("href"))
            ],
        )


__all__ = ["MetadataBacking"]
