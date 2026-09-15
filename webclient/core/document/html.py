"""HtmlBacking: tree ops for html/xml (select/attr/text_content) plus the
markdown / text / elements / links render helpers -- all html-only."""

from __future__ import annotations

import copy
import re
from typing import TYPE_CHECKING, Any, Literal, overload
from urllib.parse import urljoin

from ...collection import Field
from ..reference import Reference, from_url
from ..web_core import Backing
from .models import Element

if TYPE_CHECKING:
    from . import Document

_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_SKIP = {"script", "style"}
_NOISE = "script, style, nav, aside, footer, header"
_MAIN = "main, article, [role=main], #content, #main"


def _norm(text: str) -> str:
    return " ".join(text.split())


def tree(core: "Document") -> Any:
    """The parsed lxml root for a document (an element sub-core is its own
    element; otherwise parse ``content`` once and cache it on the core). Shared
    by ``HtmlBacking`` and the summary facets.

    XML (``kind == "xml"``) is parsed with the XML parser (namespaces, tag case
    and CDATA preserved), not the HTML parser. Both are handed the raw *bytes*, so
    lxml honours an in-document ``<meta charset>`` / BOM / ``<?xml encoding?>``
    rather than a pre-decoded string (which would mojibake a non-UTF-8 page)."""
    if core._element is not None:
        return core._element
    if core._tree is None:
        from lxml import etree, html as _lh

        raw = core.content or b""
        if core.kind == "xml":
            # libxml2 reads the in-document ``<?xml encoding?>`` declaration natively
            # from the bytes (namespaces/CDATA preserved).
            parsed = etree.fromstring(
                raw or b"<root/>", parser=etree.XMLParser(recover=True)
            )
            core._tree = parsed if parsed is not None else etree.fromstring(b"<root/>")
        else:
            # decode with the right charset, then hand lxml a str -- so a Python codec
            # name (``latin-1``/``windows-1251``/``shift_jis``) that libxml2's own
            # parser would reject still works.
            core._tree = _lh.fromstring(_html_text(core, raw) or "<html></html>")
    return core._tree


_CHARSET_RE = re.compile(rb"""(?:charset|encoding)\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.I)


def _sniff_charset(raw: bytes) -> str | None:
    """The in-document charset from a ``<meta>`` declaration or BOM in the first 2 KB,
    else ``None``. Mirrors a browser's encoding prescan; only consulted when no HTTP
    charset was sent."""
    if raw[:3] == b"\xef\xbb\xbf":
        return "utf-8"
    m = _CHARSET_RE.search(raw[:2048])
    return m.group(1).decode("ascii", "ignore") if m else None


def _html_text(core: "Document", raw: bytes) -> str:
    """Decode HTML bytes with the correct charset (WHATWG precedence: HTTP
    ``Content-Type`` charset, else an in-document ``<meta charset>`` / BOM, else
    utf-8 with a latin-1 fallback). Invalid/unknown charset names degrade, never
    raise."""
    enc = core.encoding or _sniff_charset(raw)
    if enc:
        try:
            return raw.decode(enc, "replace")
        except LookupError:
            pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", "replace")


def _tag(el: Any) -> str:
    return el.tag.lower() if isinstance(el.tag, str) else ""


def _inline(el: Any) -> str:
    parts = [el.text or ""]
    for child in el:
        tag = _tag(child)
        if tag in _SKIP or tag in ("ul", "ol", "table"):
            # block children are rendered by _md_blocks, not inlined -- keep the
            # tail text but do not concatenate the block's own text here.
            parts.append(child.tail or "")
            continue
        inner = _inline(child)
        if tag == "a":
            parts.append(f"[{inner}]({child.get('href', '')})")
        elif tag in ("strong", "b"):
            parts.append(f"**{inner}**")
        elif tag in ("em", "i"):
            parts.append(f"*{inner}*")
        elif tag == "code":
            parts.append(f"`{inner}`")
        elif tag == "img":
            parts.append(f"![{child.get('alt', '')}]({child.get('src', '')})")
        else:
            parts.append(inner)
        parts.append(child.tail or "")
    return _norm("".join(parts))


def _list_md(el: Any, depth: int) -> list[str]:
    """Markdown for a ``ul``/``ol``, recursing into nested lists with indentation.
    Each item's own text comes from ``_inline`` (which skips its child lists)."""
    lines: list[str] = []
    ordered = _tag(el) == "ol"
    idx = 0
    for li in el:
        if _tag(li) != "li":
            continue
        idx += 1
        marker = f"{idx}." if ordered else "-"
        lines.append("  " * depth + f"{marker} {_inline(li)}".rstrip())
        for sub in li:
            if _tag(sub) in ("ul", "ol"):
                lines.extend(_list_md(sub, depth + 1))
    return lines


def _table_md(table: Any) -> str:
    """A GFM pipe table: the first row is the header, the rest the body (ragged
    rows are padded). Cells are the element's collapsed text."""
    rows: list[list[str]] = []
    for tr in table.iter("tr"):
        cells = [_norm("".join(c.itertext())) for c in tr if _tag(c) in ("td", "th")]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    md = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    for r in rows[1:]:
        md.append("| " + " | ".join(r) + " |")
    return "\n".join(md)


def _md_blocks(el: Any, out: list[str]) -> None:
    for child in el:
        tag = _tag(child)
        if tag in _SKIP:
            continue
        if tag in _HEADINGS:
            out.append("#" * int(tag[1]) + " " + _inline(child))
        elif tag == "p":
            out.append(_inline(child))
        elif tag in ("ul", "ol"):
            lines = _list_md(child, 0)
            if lines:
                out.append("\n".join(lines))
        elif tag == "table":
            table = _table_md(child)
            if table:
                out.append(table)
        elif tag == "pre":
            out.append("```\n" + "".join(child.itertext()).strip("\n") + "\n```")
        elif tag == "blockquote":
            out.append("> " + _inline(child))
        elif tag == "img":
            out.append(f"![{child.get('alt', '')}]({child.get('src', '')})")
        else:
            _md_blocks(child, out)


def _main_container(root: Any) -> Any:
    found = root.cssselect(_MAIN)
    return found[0] if found else root


def _html_elements(root: Any) -> list[Element]:
    out: list[Element] = []
    counter = 0
    section: str | None = None

    def next_id() -> str:
        nonlocal counter
        counter += 1
        return f"e{counter}"

    def walk(el: Any) -> None:
        nonlocal section
        for child in el:
            tag = _tag(child)
            if tag in _SKIP:
                continue
            if tag in _HEADINGS:
                section = next_id()
                out.append(
                    Element(
                        id=section, type="title", text=_norm("".join(child.itertext()))
                    )
                )
            elif tag in ("p", "li"):
                out.append(
                    Element(
                        id=next_id(),
                        type="text" if tag == "p" else "list_item",
                        text=_norm("".join(child.itertext())),
                        parent_id=section,
                    )
                )
            elif tag == "pre":
                out.append(
                    Element(
                        id=next_id(),
                        type="code",
                        text=_norm("".join(child.itertext())),
                        parent_id=section,
                    )
                )
            elif tag == "img":
                out.append(
                    Element(
                        id=next_id(),
                        type="image",
                        text=child.get("alt", ""),
                        parent_id=section,
                        metadata={"src": child.get("src", "")},
                    )
                )
            else:
                walk(child)

    walk(root)
    return out


def _decode(core: "Document") -> str:
    """Decode the response bytes: the declared encoding first, else utf-8 with
    a latin-1 fallback (covers undeclared single-byte pages)."""
    if isinstance(core._element, str):
        return core._element
    if core.encoding:
        return (core.content or b"").decode(core.encoding, "replace")
    try:
        return (core.content or b"").decode("utf-8")
    except UnicodeDecodeError:
        return (core.content or b"").decode("latin-1", "replace")


def _miss(parent: "Document", message: str, error: Any) -> "Document":
    """A missing selection: raise a structured ``SelectError`` under RAISE, else a
    not-ok sub-document. ``SelectError`` is both a ``WebException`` (so one
    ``except WebException`` covers fetch failures and misses alike) and a
    ``LookupError`` (back-compat)."""
    from ...errors import RAISE, WebError, current_policy, select_error

    if (error or current_policy()) is RAISE:
        raise select_error(message)
    sub = parent._sub(None)
    sub.error = WebError(type="LookupError", message=message)
    return sub


class HtmlBacking(Backing):
    """Tree ops for html/xml. ``select``/``select_all`` yield element
    Documents; ``text_content`` reads the element's decoded text (all
    descendant text, tags stripped -- the DOM ``textContent``); ``attr`` reads a
    real HTML attribute."""

    provides = frozenset({"select", "select_all", "attr", "render"})
    props = frozenset({"text_content", "title"})
    gate = "tree"

    def applies(self, core: "Document") -> bool:
        return core.kind in ("html", "xml")

    def title(self, core: "Document") -> str | None:
        node = self._find(core, "title")
        return _norm("".join(node[0].itertext())) if node else None

    @overload
    def render(
        self, core: "Document", format: Literal["elements"]
    ) -> "list[Element]": ...  # noqa: E501
    @overload
    def render(
        self, core: "Document", format: Literal["links"]
    ) -> "list[Reference]": ...  # noqa: E501
    @overload
    def render(self, core: "Document", format: str, **options: Any) -> str: ...

    def render(self, core: "Document", format: str, **options: Any) -> Any:
        if format == "html":
            return (core.content or b"").decode(core.encoding or "utf-8", "replace")
        root = self._tree(core)
        if format == "links":
            return [
                from_url(urljoin(core.final_url or core.url, el.get("href")))
                for el in root.cssselect("a[href]")
                if el.get("href")
            ]
        if format == "markdown":
            target = _main_container(root) if options.get("main_content_only") else root
            blocks: list[str] = []
            _md_blocks(target, blocks)
            return "\n\n".join(b for b in blocks if b.strip())
        if format == "text":
            target = copy.deepcopy(_main_container(root))
            for noise in target.cssselect(_NOISE):
                if noise.getparent() is not None:
                    noise.getparent().remove(noise)
            blocks = [
                _norm("".join(el.itertext()))
                for el in target.iter()
                if _tag(el) in _HEADINGS or _tag(el) in ("p", "li", "pre", "blockquote")
            ]
            return "\n\n".join(b for b in blocks if b) or _norm(
                "".join(target.itertext())
            )
        if format == "elements":
            return _html_elements(root)
        raise LookupError(f"no html render format {format!r}")

    def _tree(self, core: "Document") -> Any:
        return tree(core)

    def _find(self, core: "Document", selector: str) -> list[Any]:
        if selector.rstrip().endswith(("text()",)) or "/@" in selector:
            raise ValueError(
                "select yields elements; use .attr() for an attribute or text"
            )
        root = self._tree(core)
        if selector.startswith("/") or selector.startswith("./"):
            return list(root.xpath(selector))
        return list(root.cssselect(selector))

    def select(
        self,
        core: "Document",
        selector: str,
        *,
        index: int = 0,
        optional: bool = False,
        error: Any = None,
    ) -> "Document":
        from ...errors import RETURN

        els = self._find(core, selector)
        if not (-len(els) <= index < len(els)):
            return _miss(core, f"no match for {selector!r}", RETURN if optional else error)
        return core._sub(els[index])

    def select_all(
        self,
        core: "Document",
        selector: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> "list[Document]":
        els = self._find(core, selector)[offset:]
        if limit is not None:
            els = els[:limit]
        return [core._sub(el) for el in els]

    @overload  # link attrs narrow to a Reference (overlaps the str overload)
    def attr(
        self, core: "Document", name: Literal["href", "src", "action"]
    ) -> "Reference": ...  # type: ignore[overload-overlap]  # noqa: E501
    @overload
    def attr(
        self, core: "Document", name: str, *, optional: bool = False, error: Any = None
    ) -> "Field[str]": ...  # noqa: E501

    def attr(
        self, core: "Document", name: str, *, optional: bool = False, error: Any = None
    ) -> Any:
        if core._missing:
            # honour the declared type: a link attr is a Reference even on a miss
            # (an empty, not-ok one whose ``.url`` is "" -- never a Field, so
            # ``select(..., optional=True).attr("href").url`` can't AttributeError).
            if name in ("href", "src", "action"):
                ref = from_url("")
                ref._client = core._client
                return ref
            return Field(None, ok=False)
        el = core._element
        value = el.get(name) if el is not None else None
        if name in ("href", "src", "action"):
            ref = from_url(urljoin(core.final_url or core.url, value or ""))
            ref._client = core._client  # inherit the client so it resolves
            return ref
        if value is None:  # absent attribute
            from ...errors import RAISE, current_policy, select_error

            if not optional and (error or current_policy()) is RAISE:
                raise select_error(f"no attribute {name!r}")
            return Field(None, ok=False)
        return Field(value)

    def text_content(self, core: "Document") -> "str | None":
        if core._missing:
            return None
        el = core._element if core._element is not None else self._tree(core)
        return _norm("".join(el.itertext()))


__all__ = ["HtmlBacking"]
