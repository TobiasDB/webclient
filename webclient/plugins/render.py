"""Core renderers: representations per document kind (ISSUES #16, #26).

    html -> markdown, text (readable heuristic), elements, links, html
    json -> elements

Pure functions over the parsed document. The readable-text heuristic is the
accepted stand-in for a readability port (ISSUES #26); swapping it for a
better one is plugin registration, not surgery.
"""
from __future__ import annotations

import copy
from typing import Any, Literal

from ..models import Document, Element
from .base import Renderer

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


class HtmlRenderer(Renderer):
    name: str = "core-render-html"
    kind: Literal["html", "json", "xml", "binary"] = "html"
    formats: list[str] = ["markdown", "text", "elements", "links", "html"]

    def render(self, document: Document, format: str, **options: Any) -> Any:
        if format == "html":
            return document.text
        if format == "links":
            return document.html.links()
        root = document._parsed()
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
            blocks = []
            for el in target.iter():
                tag = _tag(el)
                if tag in _HEADINGS or tag in ("p", "li", "pre", "blockquote"):
                    text = _norm("".join(el.itertext()))
                    if text:
                        blocks.append(text)
            return "\n\n".join(blocks) or _norm("".join(target.itertext()))
        if format == "elements":
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
                                           text=child.get("alt", ""),
                                           parent_id=section,
                                           metadata={"src": child.get("src", "")}))
                    else:
                        walk(child)

            walk(root)
            return out
        raise LookupError(f"HtmlRenderer has no format {format!r}")


class JsonRenderer(Renderer):
    name: str = "core-render-json"
    kind: Literal["html", "json", "xml", "binary"] = "json"
    formats: list[str] = ["elements"]

    def render(self, document: Document, format: str, **options: Any) -> Any:
        if format != "elements":
            raise LookupError(f"JsonRenderer has no format {format!r}")
        data = document.json.data
        out: list[Element] = []

        def walk(value: Any, path: str, parent: str | None) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    walk(item, f"{path}.{key}" if path else key, path or None)
            elif isinstance(value, list):
                for i, item in enumerate(value):
                    walk(item, f"{path}[{i}]", path or None)
            else:
                out.append(Element(id=path, type="text", text=str(value),
                                   parent_id=parent))

        walk(data, "", None)
        return out


def core_renderers() -> list[Renderer]:
    return [HtmlRenderer(), JsonRenderer()]


_default_table: dict[tuple[str, str], Renderer] | None = None


def default_render_table() -> dict[tuple[str, str], Renderer]:
    """Render table used by unbound documents -- pure rendering works
    without a WebClient."""
    global _default_table
    if _default_table is None:
        _default_table = {}
        for renderer in core_renderers():
            for fmt in renderer.formats:
                _default_table[(renderer.kind, fmt)] = renderer
    return _default_table
