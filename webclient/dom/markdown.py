"""HTML tree -> markdown / readable text. Pure functions over an lxml element."""

from __future__ import annotations

import copy
from typing import Any

from .parse import norm as _norm, tag as _tag

__all__ = ["HEADINGS", "SKIP", "NOISE", "MAIN", "inline", "list_md", "table_md", "md_blocks",
           "main_container", "to_markdown", "to_text"]

HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
SKIP = {"script", "style"}
NOISE = "script, style, nav, aside, footer, header"
MAIN = "main, article, [role=main], #content, #main"


def inline(el: Any) -> str:
    """The element's inline text -- its own and descendants' text with nested block/list/table
    children skipped -- collapsed to a single line (used building the markdown/skeleton view)."""
    parts = [el.text or ""]
    for child in el:
        tag = _tag(child)
        if tag in SKIP or tag in ("ul", "ol", "table"):
            # block children are rendered by md_blocks, not inlined -- keep the
            # tail text but do not concatenate the block's own text here.
            parts.append(child.tail or "")
            continue
        inner = inline(child)
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


def list_md(el: Any, depth: int) -> list[str]:
    """Markdown for a ``ul``/``ol``, recursing into nested lists with indentation.
    Each item's own text comes from ``inline`` (which skips its child lists)."""
    lines: list[str] = []
    ordered = _tag(el) == "ol"
    idx = 0
    for li in el:
        if _tag(li) != "li":
            continue
        idx += 1
        marker = f"{idx}." if ordered else "-"
        lines.append("  " * depth + f"{marker} {inline(li)}".rstrip())
        for sub in li:
            if _tag(sub) in ("ul", "ol"):
                lines.extend(list_md(sub, depth + 1))
    return lines


def table_md(table: Any) -> str:
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


def md_blocks(el: Any, out: list[str]) -> None:
    """Walk an element's children and append their Markdown block forms (headings, lists,
    tables, paragraphs) to ``out`` -- the recursive core of the HTML-to-Markdown rendering."""
    for child in el:
        tag = _tag(child)
        if tag in SKIP:
            continue
        if tag in HEADINGS:
            out.append("#" * int(tag[1]) + " " + inline(child))
        elif tag == "p":
            out.append(inline(child))
        elif tag in ("ul", "ol"):
            lines = list_md(child, 0)
            if lines:
                out.append("\n".join(lines))
        elif tag == "table":
            table = table_md(child)
            if table:
                out.append(table)
        elif tag == "pre":
            out.append("```\n" + "".join(child.itertext()).strip("\n") + "\n```")
        elif tag == "blockquote":
            out.append("> " + inline(child))
        elif tag == "img":
            out.append(f"![{child.get('alt', '')}]({child.get('src', '')})")
        else:
            md_blocks(child, out)



def main_container(root: Any) -> Any:
    """The page's main-content element (``<main>``/``role=main``/article), or the whole
    root when there is no distinct main region."""
    found = root.cssselect(MAIN)
    return found[0] if found else root



def to_markdown(root: Any, *, main_content_only: bool = False) -> str:
    """The tree as markdown (headings, paragraphs, lists, tables, code, images); with
    ``main_content_only`` only the main-content landmark is rendered."""
    target = main_container(root) if main_content_only else root
    blocks: list[str] = []
    md_blocks(target, blocks)
    return "\n\n".join(b for b in blocks if b.strip())


def to_text(root: Any, *, main_content_only: bool = True) -> str:
    """The tree's readable text: block elements one per paragraph, with nav/aside/footer/
    header chrome and scripts removed (from a COPY -- the tree is never mutated)."""
    target = copy.deepcopy(main_container(root) if main_content_only else root)
    for noise in target.cssselect(NOISE):
        if noise.getparent() is not None:
            noise.getparent().remove(noise)
    blocks = [
        _norm("".join(el.itertext()))
        for el in target.iter()
        if _tag(el) in HEADINGS or _tag(el) in ("p", "li", "pre", "blockquote")
    ]
    return "\n\n".join(b for b in blocks if b) or _norm("".join(target.itertext()))
