"""HtmlBacking: tree ops for html/xml (select/attr/text_content) plus the
markdown / text / elements / links render helpers -- all html-only."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any, Literal, overload
from urllib.parse import urljoin

from ...collection import Field
from ..reference import ReferenceCore, from_url
from ..web_core import Backing
from ._shared import Element, _element

if TYPE_CHECKING:
    from . import DocumentCore

_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_SKIP = {"script", "style"}
_NOISE = "script, style, nav, aside, footer, header"
_MAIN = "main, article, [role=main], #content, #main"


def _norm(text: str) -> str:
    return " ".join(text.split())


def tree(core: "DocumentCore") -> Any:
    """The parsed lxml root for a document (an element sub-core is its own
    element; otherwise parse ``content`` once and cache it on the core). Shared
    by ``HtmlBacking`` and the summary facets."""
    if core._element is not None:
        return core._element
    if core._tree is None:
        from lxml import html as _lh

        core._tree = _lh.fromstring(_decode(core) or "<html></html>")
    return core._tree


def _tag(el: Any) -> str:
    return el.tag.lower() if isinstance(el.tag, str) else ""


def _inline(el: Any) -> str:
    parts = [el.text or ""]
    for child in el:
        tag = _tag(child)
        if tag in _SKIP:
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
            items = [
                f"{'-' if tag == 'ul' else str(i + 1) + '.'} {_inline(li)}"
                for i, li in enumerate(child.findall("li"))
            ]
            if items:
                out.append("\n".join(items))
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


def _decode(core: "DocumentCore") -> str:
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


def _miss(parent: "DocumentCore", message: str, error: Any) -> "DocumentCore":
    """A missing selection: raise under RAISE, else a not-ok sub-document."""
    from ...errors import RAISE, WebError, current_policy

    if (error or current_policy()) is RAISE:
        raise LookupError(message)
    sub = _element(parent, None)
    sub.error = WebError(type="LookupError", message=message)
    return sub


class HtmlBacking(Backing):
    """Tree ops for html/xml. ``select``/``select_all`` yield element
    DocumentCores; ``text_content`` reads the element's decoded text (all
    descendant text, tags stripped -- the DOM ``textContent``); ``attr`` reads a
    real HTML attribute."""

    provides = frozenset({"select", "select_all", "attr", "render"})
    props = frozenset({"text_content", "title"})
    gate = "tree"

    def applies(self, core: "DocumentCore") -> bool:
        # getattr: a registered backing (``wc.use``) is probed against every core
        # the client owns, including client/session cores that have no ``kind``.
        return getattr(core, "kind", None) in ("html", "xml")

    def title(self, core: "DocumentCore") -> str | None:
        node = self._find(core, "title")
        return _norm("".join(node[0].itertext())) if node else None

    @overload
    def render(
        self, core: "DocumentCore", format: Literal["elements"]
    ) -> "list[Element]": ...  # noqa: E501
    @overload
    def render(
        self, core: "DocumentCore", format: Literal["links"]
    ) -> "list[ReferenceCore]": ...  # noqa: E501
    @overload
    def render(self, core: "DocumentCore", format: str, **options: Any) -> str: ...

    def render(self, core: "DocumentCore", format: str, **options: Any) -> Any:
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

    def _tree(self, core: "DocumentCore") -> Any:
        return tree(core)

    def _find(self, core: "DocumentCore", selector: str) -> list[Any]:
        if selector.rstrip().endswith(("text()",)) or "/@" in selector:
            raise ValueError(
                "select yields elements; use .attr() for an attribute or text"
            )
        root = self._tree(core)
        if selector.startswith("/") or selector.startswith("./"):
            return list(root.xpath(selector))
        return list(root.cssselect(selector))

    def select(
        self, core: "DocumentCore", selector: str, *, index: int = 0, error: Any = None
    ) -> "DocumentCore":
        els = self._find(core, selector)
        if not (-len(els) <= index < len(els)):
            return _miss(core, f"no match for {selector!r}", error)
        return _element(core, els[index])

    def select_all(
        self,
        core: "DocumentCore",
        selector: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> "list[DocumentCore]":
        els = self._find(core, selector)[offset:]
        if limit is not None:
            els = els[:limit]
        return [_element(core, el) for el in els]

    @overload  # link attrs narrow to a Reference (overlaps the str overload)
    def attr(
        self, core: "DocumentCore", name: Literal["href", "src", "action"]
    ) -> "ReferenceCore": ...  # type: ignore[overload-overlap]  # noqa: E501
    @overload
    def attr(
        self, core: "DocumentCore", name: str, *, error: Any = None
    ) -> "Field[str]": ...  # noqa: E501

    def attr(self, core: "DocumentCore", name: str, *, error: Any = None) -> Any:
        if core._missing:
            return Field(None, ok=False)
        el = core._element
        value = el.get(name) if el is not None else None
        if name in ("href", "src", "action"):
            ref = from_url(urljoin(core.final_url or core.url, value or ""))
            ref._client = core._client  # inherit the client so it resolves
            return ref
        if value is None:  # absent attribute
            from ...errors import RAISE, current_policy

            if (error or current_policy()) is RAISE:
                raise LookupError(f"no attribute {name!r}")
            return Field(None, ok=False)
        return Field(value)

    def text_content(self, core: "DocumentCore") -> "str | None":
        if core._missing:
            return None
        el = core._element if core._element is not None else self._tree(core)
        return _norm("".join(el.itertext()))


__all__ = ["HtmlBacking"]
