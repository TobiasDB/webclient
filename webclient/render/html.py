"""Core HTML representations: markdown, readable prose, typed blocks, raw."""
from __future__ import annotations

import copy
from typing import Any

from . import Block, RendererRegistry

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


def _blocks(el: Any, out: list[str]) -> None:
    """Emit `el` if it is a block, otherwise recurse. Written this way so a
    fragment (`<p>hi</p>` with no document wrapper) renders like a page."""
    for child in _self_or_children(el):
        tag = _tag(child)
        if tag in _SKIP:
            continue
        if tag in _HEADINGS:
            out.append("#" * int(tag[1]) + " " + _inline(child))
        elif tag == "p":
            out.append(_inline(child))
        elif tag in ("ul", "ol"):
            items = [f"{'-' if tag == 'ul' else f'{i + 1}.'} {_inline(li)}"
                     for i, li in enumerate(child.findall("li"))]
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
            _blocks(child, out)


_BLOCK_TAGS = _HEADINGS | {"p", "ul", "ol", "pre", "blockquote", "hr", "img",
                           "table", "li"}


def _self_or_children(el: Any) -> Any:
    """The root of a parsed fragment may itself be the content."""
    return [el] if _tag(el) in _BLOCK_TAGS else list(el)


def _main(root: Any) -> Any:
    found = root.cssselect(_MAIN)
    return found[0] if found else root


def markdown(document: Any, *, main_content_only: bool = False) -> str:
    root = document._backing.tree()
    target = _main(root) if main_content_only else root
    out: list[str] = []
    _blocks(target, out)
    rendered = "\n\n".join(b for b in out if b.strip())
    # A document with only inline content has no blocks; returning "" for it
    # would be technically true and practically useless.
    return rendered or _inline(target)


def readable(document: Any) -> str:
    """Prose with chrome stripped. A heuristic, deliberately — swapping in a
    real readability port is one `register` call."""
    target = copy.deepcopy(_main(document._backing.tree()))
    for noise in target.cssselect(_NOISE):
        parent = noise.getparent()
        if parent is not None:
            parent.remove(noise)
    out = []
    for el in target.iter():
        if _tag(el) in _HEADINGS or _tag(el) in ("p", "li", "pre", "blockquote"):
            text = _norm("".join(el.itertext()))
            if text:
                out.append(text)
    return "\n\n".join(out) or _norm("".join(target.itertext()))


def elements(document: Any) -> list[Block]:
    root = document._backing.tree()
    out: list[Block] = []
    counter = 0
    section: str | None = None

    def next_id() -> str:
        nonlocal counter
        counter += 1
        return f"e{counter}"

    def walk(el: Any) -> None:
        nonlocal section
        for child in _self_or_children(el):
            tag = _tag(child)
            if tag in _SKIP:
                continue
            text = _norm("".join(child.itertext()))
            if tag in _HEADINGS:
                section = next_id()
                out.append(Block(id=section, type="title", text=text))
            elif tag == "p":
                out.append(Block(id=next_id(), type="text", text=text,
                                 parent_id=section))
            elif tag == "li":
                out.append(Block(id=next_id(), type="list_item", text=text,
                                 parent_id=section))
            elif tag == "pre":
                out.append(Block(id=next_id(), type="code",
                                 text="".join(child.itertext()).strip("\n"),
                                 parent_id=section))
            elif tag == "table":
                out.append(Block(id=next_id(), type="table", text=text,
                                 parent_id=section))
            elif tag == "img":
                out.append(Block(id=next_id(), type="image",
                                 text=child.get("alt", ""), parent_id=section,
                                 metadata={"src": child.get("src", "")}))
            else:
                walk(child)

    walk(root)
    if not out:
        text = _norm("".join(root.itertext()))
        if text:
            out.append(Block(id="e1", type="text", text=text))
    return out


def raw_html(document: Any) -> str:
    return document._backing.text


def links(document: Any, *, selector: str = "a[href]") -> list[Any]:
    """Every link as a Reference, resolved against the URL that answered."""
    base = document._link_base()
    out = []
    for element in document._backing.tree().cssselect(selector):
        for name in ("href", "src", "action"):
            value = element.get(name)
            if value:
                out.append(base.join(value))
                break
    return out


def install(registry: RendererRegistry) -> None:
    for kind in ("html", "xml"):
        registry.register(kind, "markdown", markdown)
        registry.register(kind, "readable", readable)
        registry.register(kind, "elements", elements)
        registry.register(kind, "html", raw_html)
        registry.register(kind, "links", links)
