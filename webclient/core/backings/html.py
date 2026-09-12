"""HTML/XML backing: selection, attributes, and render formats.

Rendering (markdown / text / elements / links / html) lives here now -- it is
a backing op like ``select``/``attr``, dispatched by kind. A client may still
override a (kind, format) via ``wc.use(Renderer)``; the backing consults that
table first.
"""
from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any, ClassVar

from ...document import Collection, Document, Element, Field, Reference, _select_elements
from ..base import Capability
from .base import Backing

if TYPE_CHECKING:
    from ..document import DocumentCore

_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_SKIP = {"script", "style"}
_NOISE = "script, style, nav, aside, footer, header"
_MAIN = "main, article, [role=main], #content, #main"


def _tag(el: Any) -> str:
    return el.tag.lower() if isinstance(el.tag, str) else ""


def _norm(text: str) -> str:
    return " ".join(text.split())


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
            items = []
            for i, li in enumerate(child.findall("li")):
                bullet = "-" if tag == "ul" else f"{i + 1}."
                items.append(f"{bullet} {_inline(li)}")
            if items:
                out.append("\n".join(items))
        elif tag == "pre":
            out.append("```\n" + "".join(child.itertext()).strip("\n") + "\n```")
        elif tag == "blockquote":
            out.append("> " + _inline(child))
        elif tag == "hr":
            out.append("---")
        elif tag == "img":
            out.append(f"![{child.get('alt', '')}]({child.get('src', '')})")
        elif tag == "table":
            rows = []
            for tr in child.iter("tr"):
                cells = [_inline(td) for td in tr if _tag(td) in ("td", "th")]
                rows.append("| " + " | ".join(cells) + " |")
            if rows:
                out.append("\n".join(rows))
        else:
            _md_blocks(child, out)


def _main_container(root: Any) -> Any:
    found = root.cssselect(_MAIN)
    return found[0] if found else root


class HtmlBacking(Backing):
    provides = frozenset({"select", "select_all", "attr", "render"})
    gate: ClassVar[Capability] = "tree"

    def select(self, core: "DocumentCore", selector: str, *, index: int = 0,
               wait: float | None = None) -> Document:
        matches = _select_elements(core.parsed(), selector)
        try:
            return core.element(tree=matches[index])
        except IndexError:
            raise LookupError(
                f"no match for {selector!r} at index {index} "
                f"({len(matches)} matches)") from None

    def select_all(self, core: "DocumentCore", selector: str, limit: int | None = None,
                   offset: int = 0) -> Collection[Document]:
        matches = _select_elements(core.parsed(), selector)
        matches = matches[offset:offset + limit if limit is not None else None]
        out: Collection[Document] = Collection()
        out._items = [core.element(tree=m) for m in matches]
        return out

    def attr(self, core: "DocumentCore", name: str) -> Field[str] | Reference:
        if name == "text":
            return Field[str](value=core.text())
        if name == "html":
            return Field[str](value=core.doc.content.decode(core.doc.encoding or "utf-8", "replace"))
        value = core.parsed().get(name)
        if value is None:
            raise LookupError(f"no attribute {name!r} on <{core.parsed().tag}>")
        return core.doc.join(value) if name in ("href", "src", "action") else \
            Field[str](value=value)

    def render(self, core: "DocumentCore", format: str, **options: Any) -> Any:
        client = core.client
        if client is not None:
            override = client._render_table.get((core.doc.kind, format))
            if override is not None:
                return override.render(core.doc, format, **options)
        if core.page is not None and not core.is_element:
            return self._live_render(core, format, **options)   # coroutine
        return self._render_now(core, format, **options)

    async def _live_render(self, core: "DocumentCore", format: str,
                           **options: Any) -> Any:
        """A live page renders off a fresh snapshot (awaited, not bridged)."""
        await core.asnapshot()
        return self._render_now(core, format, **options)

    def _render_now(self, core: "DocumentCore", format: str, **options: Any) -> Any:
        if format == "html":
            return core.doc.content.decode(core.doc.encoding or "utf-8", "replace")
        if format == "links":
            out: Collection[Reference] = Collection()
            for el in _select_elements(core.parsed(), "a[href]"):
                href = el.get("href")
                if href:
                    out._items.append(core.doc.join(href))
            return out
        root = core.parsed()
        main_only = options.get("main_content_only", False)
        if format == "markdown":
            target = _main_container(root) if main_only else root
            blocks: list[str] = []
            _md_blocks(target, blocks)
            return "\n\n".join(b for b in blocks if b.strip())
        if format == "text":
            target = copy.deepcopy(_main_container(root))
            for noise in target.cssselect(_NOISE):
                parent = noise.getparent()
                if parent is not None:
                    parent.remove(noise)
            tblocks: list[str] = []
            for el in target.iter():
                tag = _tag(el)
                if tag in _HEADINGS or tag in ("p", "li", "pre", "blockquote"):
                    text = _norm("".join(el.itertext()))
                    if text:
                        tblocks.append(text)
            return "\n\n".join(tblocks) or _norm("".join(target.itertext()))
        if format == "elements":
            return _html_elements(root)
        raise LookupError(f"no html render format {format!r}")


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
                out.append(Element(id=section, type="title",
                                   text=_norm("".join(child.itertext()))))
            elif tag == "p":
                out.append(Element(id=next_id(), type="text",
                                   text=_norm("".join(child.itertext())),
                                   parent_id=section))
            elif tag == "li":
                out.append(Element(id=next_id(), type="list_item",
                                   text=_norm("".join(child.itertext())),
                                   parent_id=section))
            elif tag == "pre":
                out.append(Element(id=next_id(), type="code",
                                   text="".join(child.itertext()).strip("\n"),
                                   parent_id=section))
            elif tag == "table":
                out.append(Element(id=next_id(), type="table",
                                   text=_norm("".join(child.itertext())),
                                   parent_id=section))
            elif tag == "img":
                out.append(Element(id=next_id(), type="image",
                                   text=child.get("alt", ""), parent_id=section,
                                   metadata={"src": child.get("src", "")}))
            else:
                walk(child)

    walk(root)
    return out
