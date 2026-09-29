"""Content extraction over a parsed markup :class:`~web.parse.Document`.

The readable substance of a page, freed from selectors: the main article region (chrome stripped),
that region as plain text or as markdown (for an LLM / a diff / a digest), the page landmark a node
sits in, and HTML ``<table>`` rows as records with ``rowspan`` / ``colspan`` expanded. All pure
content -- no transport, no network -- so it runs on any parsed bytes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urljoin

from .nodes import Node
from .nodes import tag as _tag
from .nodes import text as _text

if TYPE_CHECKING:
    from .document import Document, Element

#: tags whose subtree is page chrome, not readable content -- dropped by the readable extractors.
_CHROME = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "nav",
        "header",
        "footer",
        "aside",
        "form",
    }
)
#: the landmark tags ``region`` reports (nearest ancestor wins).
_LANDMARKS = frozenset({"nav", "main", "article", "header", "footer", "aside"})
#: block tags that force a newline in the markdown render.
_BLOCK = frozenset(
    {
        "p",
        "div",
        "section",
        "li",
        "tr",
        "br",
        "blockquote",
        "pre",
        "hr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ul",
        "ol",
        "table",
    }
)
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})


def main_content(doc: "Document") -> "Element | None":
    """The page's main content region -- ``<main>`` / ``<article>`` / ``[role=main]`` if present,
    else the body, else the root. ``None`` for a non-markup document."""
    for css in ("main", "article", "[role=main]"):
        el = doc.select(css)
        if el is not None:
            return el
    body = doc.select("body")
    return body if body is not None else doc.select("html")


def region(el: "Element") -> str:
    """The page landmark ``el`` sits in -- ``nav`` / ``main`` / ``article`` / ``header`` /
    ``footer`` / ``aside`` -- by walking ancestors for the nearest landmark tag or ARIA role.
    """
    node: "Node | None" = el._node
    while node is not None:
        role = (node.get("role") or "").lower()
        if _tag(node) in _LANDMARKS:
            return _tag(node)
        if role in _LANDMARKS or role == "banner":
            return "header" if role == "banner" else role
        node = node.getparent()
    return ""


def _readable_root(doc: "Document", *, main_content_only: bool) -> Node:
    if main_content_only:
        el = main_content(doc)
        if el is not None:
            return el._node
    return doc._root()


def readable_text(doc: "Document", *, main_content_only: bool = True) -> str:
    """The page's readable text with chrome (scripts/nav/footer/…) removed and whitespace
    collapsed. ``main_content_only`` (default) narrows to the main region first."""
    if not doc._markup():
        return doc.text
    out: list[str] = []
    _walk_text(_readable_root(doc, main_content_only=main_content_only), out)
    return " ".join(" ".join(out).split())


def _walk_text(node: Node, out: list[str]) -> None:
    if _tag(node) in _CHROME:
        return
    if node.text:
        out.append(node.text)
    for child in node:
        _walk_text(child, out)
        if child.tail:
            out.append(child.tail)


def markdown(doc: "Document", *, main_content_only: bool = False) -> str:
    """The page rendered as markdown -- headings, links, lists, emphasis, code -- for an LLM, a
    digest, or a diff. ``main_content_only`` strips chrome to the main region first."""
    if not doc._markup():
        return doc.text
    out: list[str] = []
    _md(_readable_root(doc, main_content_only=main_content_only), out, doc.url)
    lines = [ln.rstrip() for ln in "".join(out).splitlines()]
    result: list[str] = []
    blank = 0
    for ln in lines:  # collapse runs of blank lines to one
        blank = blank + 1 if not ln else 0
        if blank <= 1:
            result.append(ln)
    return "\n".join(result).strip()


def _md(node: Node, out: list[str], base: str) -> None:
    tag = _tag(node)
    if tag in _CHROME:
        return
    if tag in _HEADINGS:
        out.append("\n" + "#" * int(tag[1]) + " " + _text(node) + "\n")
        return
    if tag == "a":
        href = node.get("href")
        out.append(f"[{_text(node)}]({urljoin(base, href)})" if href else _text(node))
        return _tail(node, out)
    if tag in ("strong", "b"):
        out.append(f"**{_text(node)}**")
        return _tail(node, out)
    if tag in ("em", "i"):
        out.append(f"*{_text(node)}*")
        return _tail(node, out)
    if tag == "code":
        out.append(f"`{_text(node)}`")
        return _tail(node, out)
    if tag == "li":
        out.append("\n- " + _text(node))
        return
    if tag == "br":
        out.append("\n")
        return
    if node.text:  # generic container / block
        out.append(node.text)
    for child in node:
        _md(child, out, base)
    if tag in _BLOCK:
        out.append("\n")


def _tail(node: Node, out: list[str]) -> None:
    if node.tail:
        out.append(node.tail)


def tables(
    doc: "Document", selector: "str | None" = None, *, transpose: bool = False
) -> "list[dict[str, str]]":
    """The rows of an HTML ``<table>`` as records keyed by header, with ``rowspan`` / ``colspan``
    EXPANDED so a merged category cell is carried into the rows it spans (something ``select_all``
    on ``<tr>`` cannot do). ``selector`` picks the table (else the one with the most rows in the
    document); ``transpose=True`` makes each COLUMN a record keyed by the first column (for a
    feature-comparison matrix whose records are columns)."""
    if not doc._markup():
        return []
    roots = [e._node for e in doc.select_all(selector)] if selector else [doc._root()]
    candidates: list[Node] = []
    for r in roots:
        candidates += (
            [r] if _tag(r) == "table" else [e for e in r.iter() if _tag(e) == "table"]
        )
    table = max(candidates, key=_row_count, default=None)
    return _table_records(table, transpose=transpose) if table is not None else []


def _row_count(table: Node) -> int:
    return sum(1 for n in table.iter() if _tag(n) == "tr")


def _table_records(table: Node, *, transpose: bool) -> "list[dict[str, str]]":
    """Expand a table into a dense grid (rowspan/colspan honoured), then key rows by the header row."""
    grid: list[dict[int, str]] = []
    pending: dict[int, tuple[str, int]] = (
        {}
    )  # col -> (text, remaining rowspan) for active rowspans
    for tr in (n for n in table.iter() if _tag(n) == "tr"):
        row: dict[int, str] = {}
        col = 0
        carry: dict[int, tuple[str, int]] = (
            {}
        )  # carry active rowspans down from earlier rows
        for c, (txt, rem) in pending.items():
            row[c] = txt
            if rem - 1 > 0:
                carry[c] = (txt, rem - 1)
        pending = carry
        for cell in (e for e in tr if _tag(e) in ("td", "th")):
            while col in row:  # skip columns already filled by a rowspan carry
                col += 1
            value = _text(cell)
            colspan = _int(cell.get("colspan"), 1)
            rowspan = _int(cell.get("rowspan"), 1)
            for i in range(colspan):
                row[col + i] = value
                if rowspan > 1:
                    pending[col + i] = (value, rowspan - 1)
            col += colspan
        grid.append(row)
    if not grid:
        return []
    width = (max(max(r) for r in grid if r) + 1) if any(grid) else 0
    matrix = [[r.get(c, "") for c in range(width)] for r in grid]
    if transpose:
        matrix = [list(col) for col in zip(*matrix)] if matrix else []
    if (
        not matrix or not matrix[0]
    ):  # no rows, or a header row with no cells -> no records
        return []
    header, *body = matrix
    keys = [h or f"col{i}" for i, h in enumerate(header)]
    return [
        {keys[i]: (r[i] if i < len(r) else "") for i in range(len(keys))} for r in body
    ]


def _int(v: "str | None", default: int) -> int:
    try:
        return int(v) if v else default
    except ValueError:
        return default


__all__ = ["main_content", "region", "readable_text", "markdown", "tables"]
