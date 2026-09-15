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


def _clean_href(value: "str | None") -> str:
    """Normalise an href/src/action value the way a browser does before resolving
    it: strip leading/trailing ASCII whitespace, drop internal tab/newline/CR, and
    percent-encode any remaining raw spaces (a space is never valid unescaped in a
    URL). Without this, a template's newlines or a text-like href such as
    ``<a href="Read More">`` builds an un-fetchable URL (``.../Read More``)."""
    if not value:
        return ""
    value = value.strip().translate({0x09: None, 0x0A: None, 0x0D: None})
    return value.replace(" ", "%20")


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
            # parser would reject still works. A blank/whitespace-only body would make
            # lxml raise "Document is empty", so fall back to an empty document.
            text = _html_text(core, raw)
            core._tree = _lh.fromstring(text if text.strip() else "<html></html>")
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
    # strip a leading BOM (browsers do; a retained U+FEFF makes lxml treat a
    # doctype-less single-block page's element as the root -> empty markdown).
    enc = core.encoding or _sniff_charset(raw)
    if enc:
        try:
            return raw.decode(enc, "replace").lstrip("﻿")
        except LookupError:  # a bogus/unknown charset name -> fall through
            pass
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", "replace")
    return text.lstrip("﻿")


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


#: tags with no selector value that only add tokens -- dropped from the skeleton.
_SKELETON_SKIP = frozenset({
    "script", "style", "noscript", "template", "svg", "path", "head", "meta",
    "link", "br", "hr", "source", "track", "wbr", "picture", "canvas", "defs",
})
#: selector-relevant attributes to surface (in this order): form/input targets,
#: accessibility + SPA test hooks -- the ones an LLM actually writes selectors on.
#: ``value`` is deliberately excluded (may be sensitive); ``href``/``src`` show as
#: presence flags below. Values are collapsed + clipped to stay token-lean.
_SKELETON_ATTRS = (
    "role", "type", "name", "placeholder", "for", "aria-label", "alt", "title",
    "data-testid", "data-test", "data-cy", "data-id", "data-qa", "contenteditable",
)
_MAX_CLASSES = 8  # cap utility-class soup (tailwind &c.) so a node stays token-lean


def _kept_children(el: Any) -> "list[Any]":
    """Child *elements* worth showing: real tags (not comments/PIs) that aren't
    structural noise."""
    return [
        c for c in el if isinstance(c.tag, str) and _tag(c) not in _SKELETON_SKIP
    ]


def _selector_sig(el: Any) -> str:
    """An HTML open-tag signature for ONE element: ``<tag id="x" class="a b"
    role="button" href>`` -- the tag with its id, (capped) classes, a few
    selector-relevant attributes (``role``/``type``/``name``/``data-testid`` …), and
    ``href``/``src`` presence (name only, not the value). Real HTML syntax an LLM
    reads natively, and everything it needs to write a CSS selector for the node.
    Classes are capped so utility-class soup can't blow up a line."""
    tag = _tag(el) or "?"
    parts = [tag]
    eid = el.get("id")
    if eid:
        parts.append(f'id="{_norm(eid)}"')
    classes = (el.get("class") or "").split()
    if classes:
        shown = " ".join(classes[:_MAX_CLASSES])
        if len(classes) > _MAX_CLASSES:
            shown += f" …+{len(classes) - _MAX_CLASSES}"
        parts.append(f'class="{shown}"')
    for attr in _SKELETON_ATTRS:
        val = el.get(attr)
        if val is not None and val != "":
            parts.append(f'{attr}="{_norm(val)[:24]}"')
    if el.get("href") is not None:  # a link/area target (presence, not the url)
        parts.append("href")
    if el.get("src") is not None:  # img/media/iframe source (presence)
        parts.append("src")
    return "<" + " ".join(parts) + ">"


_SKELETON_LEGEND = (
    '# skeleton: an HTML-tag outline (open tags only, indentation = nesting). '
    '"…"=sample text'
)


def _struct_sig(el: Any, memo: "dict[int, str]", budget: int = 6) -> str:
    """A RECURSIVE structural signature (this element + its kept children, bounded
    depth). Two siblings merge only when their structure is identical, so a
    collapsed ``… ×N`` never hides a differently-shaped sibling (e.g. an item with
    an extra badge) -- the safe, lossless form of list merging. Cached per element."""
    if budget <= 0:
        return _selector_sig(el) + "(…)"
    key = id(el)
    cached = memo.get(key)
    if cached is not None:
        return cached
    inner = ",".join(_struct_sig(k, memo, budget - 1) for k in _kept_children(el))
    sig = f"{_selector_sig(el)}({inner})"
    if budget == 6:  # only cache the full-depth signature (the one merge compares)
        memo[key] = sig
    return sig


def _xhr_endpoints(core: "Document") -> "list[str]":
    """The data-API URLs the page fetched (XHR/fetch), deduped in order -- read from
    the captured network events (only present on a browser-rendered document)."""
    from ...models import NetworkEvent

    out: list[str] = []
    seen: set[str] = set()
    for e in core._events:
        if isinstance(e, NetworkEvent) and getattr(e, "resource_type", None) in ("xhr", "fetch"):
            req = getattr(e, "request", None)
            url = str(req.dispatch("url")) if req is not None else ""
            if url and url not in seen:
                seen.add(url)
                out.append(url)
    return out


def _static_sig_set(static_html: "bytes | None") -> "frozenset[str] | None":
    """The set of ``_selector_sig`` values present in the STATIC (pre-JS) HTML, used
    to mark rendered nodes as initial vs injected. ``None`` if there is no static
    baseline (a plain ``browser="always"`` fetch, or a static-only fetch)."""
    if not static_html:
        return None
    from lxml import html as _lh

    try:
        root = _lh.fromstring(static_html)
    except Exception:  # unparseable shell -> treat everything as dynamic
        return frozenset()
    return frozenset(
        _selector_sig(el) for el in root.iter() if isinstance(el.tag, str)
    )


def _skeleton(
    root: Any,
    *,
    max_lines: int = 400,
    text_chars: int = 40,
    max_depth: int = 30,
    max_siblings: int = 200,
    legend: bool = True,
    collapse: bool = False,
    static_html: "bytes | None" = None,
    xhr_endpoints: "list[str] | None" = None,
) -> str:
    """A token-lean DOM skeleton: an indented outline of HTML open-tag signatures
    with structural noise (script/style/svg/meta/comments/…) removed and a short
    text hint on leaf nodes -- a faithful outline of the page, every sibling shown,
    so an LLM can write CSS selectors (incl. ``:nth-child``) without the raw HTML.
    Bounded by ``max_lines`` / ``max_depth`` / ``max_siblings``.

    ``collapse`` (off by default) opts into merging consecutive *structurally-
    identical* siblings to ``… ×N`` -- a uniform list of 50 cards becomes one line
    (a differently-shaped sibling is never merged away) -- for very repetitive pages
    where faithfulness costs too many tokens.

    When ``static_html`` (the pre-JS response) is supplied, a node whose signature
    is NOT in that baseline is marked ``[xhr]`` (if the page issued XHR/fetch
    requests) or ``[js]`` -- so the LLM sees which content is server-initial vs
    client-loaded."""
    lines: list[str] = []
    memo: dict[int, str] = {}
    static_sigs = _static_sig_set(static_html)
    inject_tag = " [xhr]" if xhr_endpoints else " [js]"

    def origin(el: Any) -> str:
        # only annotated when there's a static baseline to diff against.
        if static_sigs is None:
            return ""
        return "" if _selector_sig(el) in static_sigs else inject_tag

    def walk(el: Any, depth: int) -> None:
        if depth > max_depth:
            lines.append("  " * depth + "…")
            return
        children = _kept_children(el)
        i = 0
        shown = 0
        while i < len(children):
            if len(lines) >= max_lines:
                lines.append("  " * depth + "… (truncated)")
                return
            if shown >= max_siblings:
                lines.append("  " * depth + f"… ({len(children) - i} more)")
                return
            child = children[i]
            if collapse:  # merge consecutive structurally-identical siblings
                ssig = _struct_sig(child, memo)  # on STRUCTURE, not just the sig
                j = i + 1
                while j < len(children) and _struct_sig(children[j], memo) == ssig:
                    j += 1
            else:  # faithful: one line per sibling
                j = i + 1
            count = j - i
            kids = _kept_children(child)
            text = _norm("".join(child.itertext())) if not kids else ""
            hint = f'  "{text[:text_chars]}…"' if len(text) > text_chars else (
                f'  "{text}"' if text else ""
            )
            suffix = f" ×{count}" if count > 1 else ""
            lines.append("  " * depth + _selector_sig(child) + origin(child) + suffix + hint)
            walk(child, depth + 1)  # the representative's structure (all N share it)
            i = j
            shown += 1

    walk(root, 0)
    header: list[str] = []
    if legend:
        leg = _SKELETON_LEGEND
        if collapse:
            leg += ' ×N=N identical siblings collapsed;'
        if static_sigs is not None:
            leg += "  [xhr]/[js]=client-injected (unmarked=server-initial)"
        header.append(leg)
    if xhr_endpoints:
        shown_ep = xhr_endpoints[:8]
        more = f" (+{len(xhr_endpoints) - 8} more)" if len(xhr_endpoints) > 8 else ""
        header.append("# XHR/fetch data APIs: " + ", ".join(shown_ep) + more)
    return "\n".join([*header, *lines])


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


#: the page landmarks ``region`` classifies an element into (the HTML sectioning
#: elements + their ARIA-role and class/id equivalents).
_LANDMARKS = frozenset({"nav", "main", "article", "header", "footer", "aside"})

#: ARIA landmark ``role`` -> the landmark it denotes.
_LANDMARK_ROLES = {
    "navigation": "nav",
    "main": "main",
    "article": "article",
    "banner": "header",
    "contentinfo": "footer",
    "complementary": "aside",
}

#: class / id substring hints, tried (in order) when an ancestor has no landmark
#: tag or ARIA role -- the first hit classifies the region.
_LANDMARK_HINTS = (
    ("footer", "footer"),
    ("masthead", "header"),
    ("breadcrumb", "nav"),
    ("menu", "nav"),
    ("nav", "nav"),
    ("sidebar", "aside"),
)


class HtmlBacking(Backing):
    """Tree ops for html/xml. ``select``/``select_all`` yield element
    Documents; ``text_content`` reads the element's decoded text (all
    descendant text, tags stripped -- the DOM ``textContent``); ``attr`` reads a
    real HTML attribute; ``region`` reports which page landmark an element sits
    in."""

    provides = frozenset(
        {"select", "select_all", "attr", "render",
         "markdown", "text", "html", "links", "elements", "skeleton"}
    )
    collections = frozenset({"select_all", "links"})  # return a Collection of cores
    props = frozenset({"text_content", "title", "region"})
    gate = "tree"

    def region(self, core: "Document") -> str:
        """The page landmark this element sits in -- ``nav`` / ``main`` /
        ``article`` / ``header`` / ``footer`` / ``aside`` (or ``""`` if none) --
        found by walking its ancestors for the nearest landmark tag, ARIA
        ``role``, or class/id hint. A standard-web-semantics primitive: "is this
        link in the nav, the article, or the footer?" (a crawl scores links by
        it; an LLM can filter on it)."""
        node = core._element
        hops = 0
        while node is not None and hops < 25:
            tag = _tag(node).rsplit("}", 1)[-1]  # strip any XML namespace
            if tag in _LANDMARKS:
                return tag
            role = (node.get("role") or "").strip().lower() if hasattr(node, "get") else ""
            if role in _LANDMARK_ROLES:
                return _LANDMARK_ROLES[role]
            hint = (
                f"{node.get('class') or ''} {node.get('id') or ''}".lower()
                if hasattr(node, "get")
                else ""
            )
            if hint.strip():
                for needle, landmark in _LANDMARK_HINTS:
                    if needle in hint:
                        return landmark
            node = node.getparent()
            hops += 1
        return ""

    # -- named render front doors (typed sugar over ``render(format)``) --------
    def markdown(self, core: "Document", *, main_content_only: bool = False) -> str:
        """The page as markdown (``render("markdown")`` with a proper ``str`` type
        and no stringly-typed format arg)."""
        return self.render(core, "markdown", main_content_only=main_content_only)

    def text(self, core: "Document", *, main_content_only: bool = True) -> str:
        """The page's readable text (nav/chrome stripped by default)."""
        return self.render(core, "text", main_content_only=main_content_only)

    def html(self, core: "Document") -> str:
        """The raw decoded HTML source."""
        return self.render(core, "html")

    def links(self, core: "Document") -> "list[Reference]":
        """The page's outbound links as References."""
        return self.render(core, "links")

    def elements(self, core: "Document") -> "list[Element]":
        """The page as a flat list of typed content blocks."""
        return self.render(core, "elements")

    def skeleton(
        self,
        core: "Document",
        *,
        max_lines: int = 400,
        text_chars: int = 40,
        max_depth: int = 30,
        max_siblings: int = 200,
        legend: bool = True,
        collapse: bool = False,
        annotate_origin: bool = True,
    ) -> str:
        """A token-lean DOM skeleton -- an indented HTML open-tag outline with the
        bloat (scripts/styles/svg/…) removed and leaf text hinted, every sibling
        shown faithfully. Keeps every id and class path so an LLM can write CSS
        selectors for the page cheaply (feed this instead of raw HTML, then use the
        selectors with ``select``/``select_all``/``extract``). ``collapse=True``
        merges structurally-identical siblings to ``×N`` for very repetitive pages.

        On a browser-rendered document (``browser="probe"``/``"auto"``) with a static
        baseline, nodes that were NOT in the server's initial HTML are marked
        ``[xhr]`` (if the page issued XHR/fetch calls) or ``[js]``, and observed data
        APIs are listed -- so the LLM sees what is server-initial vs client-loaded."""
        static_html = core._static_html if annotate_origin else None
        xhr = _xhr_endpoints(core) if annotate_origin else None
        return _skeleton(
            self._tree(core),
            max_lines=max_lines,
            text_chars=text_chars,
            max_depth=max_depth,
            max_siblings=max_siblings,
            legend=legend,
            collapse=collapse,
            static_html=static_html,
            xhr_endpoints=xhr,
        )

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
            return _html_text(core, core.content or b"")  # graceful on a bogus charset
        root = self._tree(core)
        if format == "links":
            base = core.final_url or core.url
            return [
                from_url(urljoin(base, href))
                for el in root.cssselect("a[href]")
                if (href := _clean_href(el.get("href")))
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
        if format == "skeleton":
            return self.skeleton(core, **options)
        from ...errors import render_error

        raise render_error(f"no html render format {format!r}")

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
        # ``attr`` is the one element accessor: a real HTML attribute, plus the
        # pseudo-attributes ``"text"`` (the element's text -- same as ``text_content``)
        # and ``"html"`` (its markup). So "give me X from this node" is always
        # ``attr("x")`` -- no separate op to remember, and ``attr("text")`` yields text
        # instead of a silent miss.
        if name == "text":
            return Field(self.text_content(core))
        if name == "html":
            return Field(None if core._missing else self.render(core, "html"))
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
            ref = from_url(urljoin(core.final_url or core.url, _clean_href(value)))
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
