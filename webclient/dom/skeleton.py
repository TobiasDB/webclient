"""The token-lean DOM skeleton: an indented outline of HTML open-tag signatures, plus the
per-element signature helpers the record detector and the element index share.

Pure over an lxml tree. The optional enrichments (origin marks, XHR correlation, record
marks, interactivity marks) are passed IN as plain data -- a :class:`CorrelationLike` for
the request timeline, a ``region_marks`` dict, a flag -- so this module knows nothing about
documents, events or browsers.
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol, runtime_checkable

from .classes import semantic_classes
from .interactivity import interactive
from .json import json_skeleton
from .parse import norm as _norm, parse_html, tag as _tag
from .records import mark_for

__all__ = ["CorrelationLike", "kept_children", "selector_sig", "struct_sig", "static_sig_set",
           "json_islands", "is_chrome", "skeleton", "SKELETON_SKIP"]


@runtime_checkable
class CorrelationLike(Protocol):
    """What the skeleton reads from an XHR->DOM correlation: the request timeline and, per
    stamped node, the candidate request indices / the revealing action."""

    @property
    def requests(self) -> "list[Any]": ...
    def candidates_for(self, node_key: str) -> "list[int]": ...
    def action_for(self, node_key: str) -> "int | None": ...


#: tags with no selector value that only add tokens -- dropped from the skeleton.
SKELETON_SKIP = frozenset({
    "script", "style", "noscript", "template", "svg", "path", "head", "meta",
    "link", "br", "hr", "source", "track", "wbr", "picture", "canvas", "defs",
})
#: selector-relevant attributes to surface (in this order): form/input targets,
#: accessibility + SPA test hooks -- the ones an LLM actually writes selectors on.
#: A form input's ``value`` stays hidden (may be sensitive); a DISPLAY ``value``
#: (``<data>``/``<meter>``/…) is surfaced separately below. ``href``/``src`` show as
#: presence flags below. Values are collapsed + clipped to stay token-lean.
_SKELETON_ATTRS = (
    "role", "type", "name", "placeholder", "for", "aria-label", "alt", "title",
    "data-testid", "data-test", "data-cy", "data-id", "data-qa", "contenteditable",
)
_SKELETON_ATTRS_SET = frozenset(_SKELETON_ATTRS)  # for de-duping the value-bearing data-* scan
_MAX_CLASSES = 8  # cap utility-class soup (tailwind &c.) so a node stays token-lean

#: VALUE-BEARING attributes -- where a field's value lives in an attribute, not the text
#: (``<time datetime>``, ``<meta content>``). Surfaced WITH their value so the LLM sees to
#: read the attribute, not the (often empty / formatted) text. Pairs with ``attr(name)``.
_VALUE_ATTRS = ("datetime", "content")
#: tags whose ``value`` is a DISPLAY value (safe to show), unlike a form input's ``value``
#: (excluded as possibly sensitive): ``<data>``/``<meter>``/``<progress>``/``<option>``/``<li>``.
_VALUE_TAGS = frozenset({"data", "meter", "progress", "option", "li"})
#: value-bearing ``data-*`` names (``data-price``/``data-rating``/…) -- worth showing with
#: their value; generic/analytics ``data-*`` (``data-ga-id`` …) are left out as noise.
_VALUE_DATA_RE = re.compile(
    r"^data-(price|value|amount|cost|total|rating|score|rank|count|qty|quantity|"
    r"stock|date|time|sku|code|number|num|id|key|index|state|status)$"
)

#: tags whose interactivity is SELF-EVIDENT -- marking them "clickable" would be noise. The
#: skeleton only flags NON-obvious controls (a div/span made clickable via role/onclick/…).
_OBVIOUS_INTERACTIVE = frozenset({
    "a", "button", "input", "select", "textarea", "summary", "label", "option", "details",
})


def kept_children(el: Any) -> "list[Any]":
    """Child *elements* worth showing: real tags (not comments/PIs) that aren't
    structural noise."""
    return [
        c for c in el if isinstance(c.tag, str) and _tag(c) not in SKELETON_SKIP
    ]


def selector_sig(el: Any) -> str:
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
    classes = semantic_classes(str(el.get("class") or "").split())  # drop hashed build classes
    if classes:
        shown = " ".join(classes[:_MAX_CLASSES])
        if len(classes) > _MAX_CLASSES:
            shown += f" …+{len(classes) - _MAX_CLASSES}"
        parts.append(f'class="{shown}"')
    for attr in _SKELETON_ATTRS:
        val = el.get(attr)
        if val is not None and val != "":
            parts.append(f'{attr}="{_norm(val)[:24]}"')
    # value-bearing attributes: surface WITH their value, so the LLM sees the field lives in
    # an attribute (a machine date in `datetime`, a price in `content`/`data-price`), not text.
    for attr in _VALUE_ATTRS:
        val = el.get(attr)
        if val is not None and val != "":
            parts.append(f'{attr}="{_norm(val)[:24]}"')
    if tag in _VALUE_TAGS:  # a DISPLAY value (not a form input's -- those stay hidden)
        v = el.get("value")
        if v is not None and v != "":
            parts.append(f'value="{_norm(v)[:24]}"')
    shown_data = 0  # value-bearing data-* (price/rating/…), capped; skip the ones already shown
    for name, v in (el.attrib.items() if hasattr(el, "attrib") else []):
        if shown_data >= 3:
            break
        if (name not in _SKELETON_ATTRS_SET and not name.startswith("data-wc-")
                and _VALUE_DATA_RE.match(name) and v):
            parts.append(f'{name}="{_norm(str(v))[:24]}"')
            shown_data += 1
    if el.get("href") is not None:  # a link/area target (presence, not the url)
        parts.append("href")
    if el.get("src") is not None:  # img/media/iframe source (presence)
        parts.append("src")
    return "<" + " ".join(parts) + ">"


_SKELETON_LEGEND = (
    '# skeleton: an HTML-tag outline (open tags only, indentation = nesting). '
    '"…"=sample text'
)


def struct_sig(el: Any, memo: "dict[int, str]", budget: int = 6) -> str:
    """A RECURSIVE structural signature (this element + its kept children, bounded
    depth). Two siblings merge only when their structure is identical, so a
    collapsed ``… ×N`` never hides a differently-shaped sibling (e.g. an item with
    an extra badge) -- the safe, lossless form of list merging. Cached per element."""
    if budget <= 0:
        return selector_sig(el) + "(…)"
    key = id(el)
    cached = memo.get(key)
    if cached is not None:
        return cached
    inner = ",".join(struct_sig(k, memo, budget - 1) for k in kept_children(el))
    sig = f"{selector_sig(el)}({inner})"
    if budget == 6:  # only cache the full-depth signature (the one merge compares)
        memo[key] = sig
    return sig


def static_sig_set(static_html: "bytes | None") -> "frozenset[str] | None":
    """The set of ``selector_sig`` values present in the STATIC (pre-JS) HTML, used
    to mark rendered nodes as initial vs injected. ``None`` if there is no static
    baseline (a plain ``browser="always"`` fetch, or a static-only fetch)."""
    if not static_html:
        return None
    try:
        root = parse_html(static_html)
    except Exception:  # unparseable shell -> treat everything as dynamic
        return frozenset()
    if root is None:  # no lxml -> no baseline to diff against
        return None
    return frozenset(
        selector_sig(el) for el in root.iter() if isinstance(el.tag, str)
    )


_JSON_SCRIPT = 'script[type="application/json"], script[type="application/ld+json"]'


def json_islands(root: Any, *, max_islands: int = 4, preview_lines: int = 12) -> list[str]:
    """Injected-JSON islands in the page: ``<script type="application/json">`` /
    ``ld+json`` blobs (a ``__NEXT_DATA__`` / catalog payload) whose records the DOM does
    NOT render. The skeleton strips scripts, so these are otherwise invisible -- surfacing
    them (a selector + a small JSON shape preview) is what tells the LLM to
    ``select("script#…").as_json()`` into the blob instead of scraping an empty shell."""
    out: list[str] = []
    try:
        scripts = root.cssselect(_JSON_SCRIPT)
    except Exception:  # noqa: BLE001 - a tree the selector engine can't run -> no islands
        return out
    for el in scripts[:max_islands]:
        raw = "".join(el.itertext()).strip()
        if len(raw) < 2:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue  # not real JSON (an inline config with JS, etc.)
        if not isinstance(data, (dict, list)):
            continue
        sid = el.get("id")
        sel = f"script#{sid}" if sid else 'script[type="application/json"]'
        shape = "\n".join("    " + ln for ln in json_skeleton(
            data, max_lines=preview_lines).splitlines()[:preview_lines])
        out.append(f"{sel}  ->  .as_json() then dotted-path in:\n{shape}")
    return out


#: page-chrome landmarks -- navigation / footer / sidebar, by tag or ARIA role. Dropped from
#: the skeleton under ``drop_chrome`` so a huge page's records aren't buried under menus. A bare
#: ``<header>`` tag is NOT dropped (an <article>/<section> header holds the record's title); only
#: an explicit ``role="banner"`` page header is.
_CHROME_TAGS = frozenset({"nav", "footer", "aside"})
_CHROME_ROLES = frozenset({"navigation", "contentinfo", "complementary", "search", "banner"})


def is_chrome(el: Any) -> bool:
    """Whether the element is page chrome (nav/footer/aside, or an ARIA landmark role) --
    the parts dropped from the skeleton under ``drop_chrome`` so records aren't buried."""
    if _tag(el).rsplit("}", 1)[-1] in _CHROME_TAGS:
        return True
    role = (el.get("role") or "").strip().lower() if hasattr(el, "get") else ""
    return role in _CHROME_ROLES


def skeleton(
    root: Any,
    *,
    max_lines: int = 400,
    text_chars: int = 40,
    max_depth: int = 30,
    max_siblings: int = 200,
    legend: bool = True,
    collapse: bool = False,
    drop_chrome: bool = False,
    static_html: "bytes | None" = None,
    xhr_endpoints: "list[str] | None" = None,
    correlation: "CorrelationLike | None" = None,
    region_marks: "dict[str, str] | None" = None,
    mark_interactive: bool = False,
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
    static_sigs = static_sig_set(static_html)
    inject_tag = " [xhr]" if xhr_endpoints else " [js]"

    def origin(el: Any) -> str:
        # only annotated when there's a static baseline to diff against.
        if static_sigs is None:
            return ""
        return "" if selector_sig(el) in static_sigs else inject_tag

    def phase_note(el: Any) -> str:
        # which XHR request(s) / action this node's content followed (the correlation
        # stamp) -- an ANNOTATION, never a selector; data-wc-node itself is never shown.
        if correlation is None:
            return ""
        node = el.get("data-wc-node") if hasattr(el, "get") else None
        if not node:
            return ""
        cands = correlation.candidates_for(node)
        action = correlation.action_for(node)
        parts = []
        if cands:
            parts.append(f"req[{', '.join(str(c) for c in cands)}]")
        if action:
            parts.append(f"act[{action}]")
        return f"  ← after {' '.join(parts)}" if parts else ""

    def record_note(el: Any) -> str:
        # flag the dominant repeating region (the dataset) with a suggested select_all;
        # matched by canonical XPath (lxml proxies have no stable id()).
        return mark_for(region_marks, el) if region_marks else ""

    def interact_note(el: Any) -> str:
        # mark NON-obvious controls: a <div>/<span>/… made clickable via role/onclick/tabindex
        # (static/semantic) OR a JS listener / cursor:pointer (the dynamic data-wc-int stamp,
        # which catches event delegation) -- the ones the tag alone doesn't reveal. hover /
        # scroll targets are marked wherever seen. A plain <a>/<button> is left alone (noise).
        if not mark_interactive:
            return ""
        kinds = set((el.get("data-wc-int") or "").split()) if hasattr(el, "get") else set()
        marks: list[str] = []
        if _tag(el) not in _OBVIOUS_INTERACTIVE:
            if "click" in kinds or (interactive(el) is not None):
                marks.append("clickable")
        if "hover" in kinds:
            marks.append("hover")
        if "scroll" in kinds:
            marks.append("scroll")
        return f"  ← {'/'.join(marks)}" if marks else ""

    def walk(el: Any, depth: int) -> None:
        if depth > max_depth:
            lines.append("  " * depth + "…")
            return
        children = kept_children(el)
        if drop_chrome:  # drop nav/footer/sidebar landmarks so records aren't buried
            children = [c for c in children if not is_chrome(c)]
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
                ssig = struct_sig(child, memo)  # on STRUCTURE, not just the sig
                j = i + 1
                while j < len(children) and struct_sig(children[j], memo) == ssig:
                    j += 1
            else:  # faithful: one line per sibling
                j = i + 1
            count = j - i
            kids = kept_children(child)
            text = _norm("".join(child.itertext())) if not kids else ""
            hint = f'  "{text[:text_chars]}…"' if len(text) > text_chars else (
                f'  "{text}"' if text else ""
            )
            suffix = f" ×{count}" if count > 1 else ""
            lines.append(
                "  " * depth + selector_sig(child) + origin(child) + phase_note(child)
                + record_note(child) + interact_note(child) + suffix + hint
            )
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
        if correlation is not None and correlation.requests:
            leg += '  "← after [n]"=this content followed request [n] below'
        if region_marks:
            leg += '  "← RECORD LIST"=the repeating dataset region (select_all target)'
        if mark_interactive:
            leg += '  "← clickable"=a non-obvious control (div/span made clickable)'
        header.append(leg)
    if correlation is not None and correlation.requests:
        header.append("# XHR/fetch requests (completion order, seconds since the first):")
        for r in correlation.requests[:12]:
            header.append(f"#  [{r.index}] {r.method} {r.url}  {r.t_s:.2f}s")
        if len(correlation.requests) > 12:
            header.append(f"#  … (+{len(correlation.requests) - 12} more)")
    if xhr_endpoints:
        shown_ep = xhr_endpoints[:8]
        more = f" (+{len(xhr_endpoints) - 8} more)" if len(xhr_endpoints) > 8 else ""
        header.append("# XHR/fetch data APIs: " + ", ".join(shown_ep) + more)
    for island in json_islands(root):  # injected-JSON blobs the DOM doesn't render
        header.append("# injected JSON island (records live here, not in the DOM): " + island)
    return "\n".join([*header, *lines])

