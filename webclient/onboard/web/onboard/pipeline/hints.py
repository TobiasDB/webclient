"""Page-reading helpers the stages share: the typical record, its structure, and the PRECISE
failure hints (closest selectors, what the record holds) -- all in the durable selector form."""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from web.parse import Document, Element
from web.parse.classes import class_hook

from ..prompts import clip

#: a record / page structure shown to the model never exceeds this (~400 tokens).
RECORD_CHARS = 1_600
#: a URL path that names ONE record (a detail page): a date path, a long slug, an id.
_DETAIL_PATH = re.compile(
    r"/\d{4}/\d{1,2}(/\d{1,2})?/|/\d{4}/[a-z0-9-]{8,}|/(?:news|article|articles|release|releases|"
    r"press-release|press-releases|story|stories|post|posts|event|events|detail|details|item|"
    r"filing|filings|blog)/[^/]*[a-z][^/]*\d|/[a-z0-9]+(?:-[a-z0-9]+){4,}/?$|/\d{5,}/?$",
    re.I,
)
_SKIP_ATTRS = frozenset({"class", "style", "onclick", "tabindex"})


def detail_shaped(url: str) -> bool:
    """Whether a URL's path names ONE record (a leaf, never a listing)."""
    return bool(_DETAIL_PATH.search(urlparse(url).path.rstrip("/") + "/"))


def registrable(url: str) -> str:
    """The registrable domain (``investors.x.com`` -> ``x.com``; ``a.co.uk`` keeps three labels)."""
    host = (urlparse(url).hostname or "").lower()
    parts = host.split(".")
    two_part = len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "ac", "gov")
    if two_part and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def leaf(el: Element) -> bool:
    return len(el.select_all("*")) <= 1


def _shape(el: Element) -> "tuple[str, ...]":
    return tuple(sorted(e.tag for e in el.select_all("*") if leaf(e) and e.text.strip()))


def typical(doc: Document, css: str) -> int:
    """The index of the first TYPICAL record among the first matches (a header row / a featured
    first item is the odd one out)."""
    els = doc.select_all(css)[:6]
    if len(els) < 2:
        return 0
    shapes = [_shape(e) for e in els]
    common = max(set(shapes), key=shapes.count)
    return next(i for i, sh in enumerate(shapes) if sh == common)


def record_structure(doc: Document, selector: str) -> str:
    """A typical record's structure (an HTML fragment's skeleton in durable selector form, or the
    first JSON item), clipped to :data:`RECORD_CHARS`."""
    if doc.kind == "json":
        val = doc.at(selector) if selector else doc.json()
        first = val[0] if isinstance(val, list) and val else val
        return clip(
            json.dumps(first, ensure_ascii=False, indent=1, default=str), RECORD_CHARS, "record"
        )
    els = doc.select_all(selector)
    if not els:
        return ""
    el = els[typical(doc, selector)]
    frag = Document(content=el.html.encode("utf-8"), kind="html", url=doc.url)
    return clip(
        frag.skeleton(max_lines=120, text_chars=60, mark_records=False, mark_interactive=False),
        RECORD_CHARS,
        "record",
    )


def tokens(text: str) -> "set[str]":
    return {t.lower() for t in re.findall(r"[A-Za-z][\w-]*", text)}


def candidates(el: Element) -> "list[str]":
    """The durable selectors an element answers to: tag, ``tag.class`` / ``tag[class*=stem]``,
    ``tag[attr]`` -- never an id (a record / a listing field is never read by id)."""
    attrs = el.attrs
    tag = el.tag or "*"
    out = [tag]
    out += [f"{tag}{h}" for c in attrs.get("class", "").split() if (h := class_hook(c))][:3]
    out += [f"{tag}[{a}]" for a in attrs if a not in _SKIP_ATTRS][:4]
    return out


def closest(scope: "Document | Element", css: str, *, limit: int = 6) -> "list[str]":
    """The selectors in ``scope`` closest to a ``css`` that missed (shared tag / class / attribute
    tokens, most shared first; the more specific wins a tie)."""
    want = tokens(css)
    scored: dict[str, int] = {}
    for el in scope.select_all("*")[:600]:
        for cand in candidates(el):
            shared = len(want & tokens(cand))
            if shared and scored.get(cand, 0) < shared:
                scored[cand] = shared
    return [c for c, _ in sorted(scored.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))][
        :limit
    ]


def leaves(scope: "Document | Element", *, limit: int = 8) -> "list[str]":
    """The text-bearing leaves of ``scope`` as ``selector ('text')`` -- what a field could read."""
    out: list[str] = []
    seen: set[str] = set()
    for el in scope.select_all("*")[:600]:
        text = " ".join(el.text.split())
        if not text or not leaf(el):
            continue
        cands = candidates(el)
        sel = cands[1] if len(cands) > 1 else el.tag
        if sel in seen:
            continue
        seen.add(sel)
        out.append(f"{sel} ({text[:40]!r})")
        if len(out) >= limit:
            break
    return out


def attrs_of(scope: "Document | Element", css: str) -> "list[str]":
    """The attribute names the first ``css`` match carries (what ``attr(...)`` could read)."""
    el = scope.select(css)
    return [a for a in el.attrs if a not in ("class", "style")] if el is not None else []


__all__ = [
    "RECORD_CHARS",
    "attrs_of",
    "candidates",
    "closest",
    "detail_shaped",
    "leaf",
    "leaves",
    "record_structure",
    "registrable",
    "tokens",
    "typical",
]
