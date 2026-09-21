"""The indexed element table -- a DOM-addressing primitive (pure half).

An agent sees a NUMBERED table of elements (index + role + name, never a class) and returns
an index; we resolve that index to a DURABLE, class-free CSS selector for the recorded Plan.
So the model reasons in indexes and never authors (or hallucinates) a selector, while the
Plan it produces still serialises and replays against a fresh render.

:func:`index_elements` builds the table from a parsed tree (``kind="interactive"`` = the
controls to click / type / wait for; ``kind="content"`` = the text-bearing / repeated-record
elements to extract, with ``repeats`` marking a member of a repeated structure).
:func:`durable_selector` is the resolver: id / name / aria-label / a stable semantic class /
a structural nth-of-type path -- never a volatile utility or hashed class.

Note: the browser accessibility tree (CDP ``Accessibility.getFullAXTree``) would be a
higher-fidelity source of role/name here and is worth revisiting as an alternate source
(same :class:`IndexedElement` shape) if the tag/aria-derived names prove too noisy.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from .classes import is_noise_class
from .interactivity import interactive
from .naming import _wordlike, name as _name
from .parse import tag, text_of
from .records import find_record_regions, scan_regions, xpath_of

__all__ = [
    "IndexedElement", "durable_selector", "index_elements", "record_options",
    "field_options", "render_table", "role_of",
]


class IndexedElement(BaseModel):
    """One entry in a document's indexed element table -- the DOM-addressing primitive an agent
    reasons over WITHOUT seeing classes or authoring a selector. ``index`` is its position in the
    numbered table; ``role`` is a coarse control/content role (button / textbox / link / text /
    row / …); ``name`` is a short human label (aria-label / text / …); ``kind`` is whether it is
    interactive or content-bearing; ``selector`` is the DURABLE, class-free CSS the loop resolves
    the index to (for the recorded Plan). ``repeats`` > 1 marks a member of a repeated structure
    (a record row), so a query agent can pick ``select_all`` knowingly."""

    index: int
    role: str = ""
    name: str = ""
    kind: Literal["interactive", "content"] = "interactive"
    selector: str = ""
    repeats: int = 1


#: coarse control roles by tag, for the table's role column (a11y-flavoured but tag-derived).
_ROLE_BY_TAG = {
    "a": "link", "button": "button", "select": "combobox", "textarea": "textbox",
    "summary": "disclosure", "label": "label", "option": "option",
}
#: input ``type`` -> role (a text-ish field is a textbox; the rest keep their type as the role).
_TEXTISH = frozenset({"text", "search", "email", "url", "tel", "password", "number", ""})


def role_of(el: Any, is_interactive: bool) -> str:
    """A coarse role for the table: an explicit ARIA ``role`` wins, else a tag/input-type
    mapping (link / button / textbox / checkbox / …), else ``control`` or ``text``."""
    aria = (el.get("role") or "").strip().lower()
    if aria:
        return aria
    t = tag(el)
    if t == "input":
        it = (el.get("type") or "text").strip().lower()
        return "textbox" if it in _TEXTISH else it
    return _ROLE_BY_TAG.get(t, "control" if is_interactive else "text")


def _esc_attr(value: str) -> str:
    """Escape a value for a ``[attr="value"]`` selector (drop the double-quote that would
    close it -- an attribute selector on a value containing one is not worth the complexity)."""
    return value.replace('"', "")


def _nth_step(el: Any) -> str:
    """One step of a structural path: the tag, plus ``:nth-of-type(n)`` when the element has
    same-tag siblings (so the step is unambiguous among them)."""
    t = tag(el)
    parent = el.getparent()
    if parent is None:
        return t
    same = [c for c in parent if tag(c) == t]
    if len(same) > 1:
        return f"{t}:nth-of-type({same.index(el) + 1})"
    return t


def durable_selector(el: Any, *, within: Any = None) -> str:
    """A durable, class-free CSS selector for ``el`` -- one that matches a FRESH render (so a
    recorded Plan replays), never a volatile utility/hashed class or the runtime ``data-wc-node``
    stamp. Preference order: a word-like ``#id``, a form-field ``[name]``, an ``[aria-label]``, a
    single STABLE semantic class, else a structural ``nth-of-type`` path. ``within`` scopes the
    path to a record subtree (a relative field selector that evaluates per row)."""
    eid = el.get("id")
    if eid and _wordlike(eid) and not is_noise_class(eid):
        return f"#{eid}"
    t = tag(el)
    nm = el.get("name")
    if nm:
        return f'{t}[name="{_esc_attr(nm)}"]'
    aria = el.get("aria-label")
    if aria and '"' not in aria:
        return f'{t}[aria-label="{_esc_attr(aria)}"]'
    stable = [c for c in (el.get("class") or "").split() if not is_noise_class(c)]
    if stable:  # a single semantic class (the longest = usually the most specific)
        return f"{t}.{sorted(stable, key=len)[-1]}"
    # structural path up to `within` (or the document root)
    steps: list[str] = []
    node = el
    while node is not None and node is not within and isinstance(getattr(node, "tag", None), str):
        steps.append(_nth_step(node))
        node = node.getparent()
    return " > ".join(reversed(steps))


def index_elements(
    root: Any, *, kind: "Literal['interactive', 'content']", limit: int = 200
) -> "list[IndexedElement]":
    """Walk ``root`` in document order and build the numbered :class:`IndexedElement` table. For
    ``kind="interactive"`` the entries are the interactive controls (:func:`interactive`); for
    ``kind="content"`` they are the text-bearing leaves, with ``repeats`` set from the detected
    record regions so a repeated row is marked. Bounded by ``limit``."""
    repeats = _repeat_counts(root) if kind == "content" else {}
    out: list[IndexedElement] = []
    for el in root.iter():
        if not isinstance(getattr(el, "tag", None), str):
            continue
        di = interactive(el)
        if kind == "interactive":
            if di is None:
                continue
            label = _name(el)
            out.append(IndexedElement(
                index=len(out) + 1, role=role_of(el, True),
                name=label.label if label else text_of(el)[:60],
                kind="interactive", selector=durable_selector(el),
            ))
        else:  # content: text-bearing leaves (skip pure containers and empty nodes)
            txt = text_of(el)
            if not txt or any(isinstance(c.tag, str) and text_of(c) for c in el):
                continue  # a container whose text comes from child elements -> not a leaf
            out.append(IndexedElement(
                index=len(out) + 1, role=role_of(el, di is not None),
                name=txt[:80], kind="content", selector=durable_selector(el),
                repeats=repeats.get(xpath_of(el), 1),
            ))
        if len(out) >= limit:
            break
    return out


def _repeat_counts(root: Any) -> "dict[str, int]":
    """Map each record MEMBER's canonical xpath -> the size of its repeated group, so a content
    element inside a repeated row is marked ``repeats=N`` (built from the record-region scan)."""
    counts: dict[str, int] = {}
    for container, region in scan_regions(root, min_items=3):
        for member in container:
            if isinstance(getattr(member, "tag", None), str):
                for node in member.iter():
                    p = xpath_of(node)
                    if p:
                        counts[p] = max(counts.get(p, 1), region.count)
    return counts


def record_options(root: Any, *, top_k: int = 5) -> "list[IndexedElement]":
    """The repeated-record REGIONS of ``root`` as numbered options -- what a query agent picks
    its ``select_all`` container from. Each entry's ``selector`` is the region's item selector
    and ``repeats`` its member count (best-scoring regions first, up to ``top_k``)."""
    out: list[IndexedElement] = []
    for region in find_record_regions(root, top_k=top_k):
        out.append(IndexedElement(
            index=len(out) + 1, role="record", name=f"{region.count} items",
            kind="content", selector=region.item_selector, repeats=region.count,
        ))
    return out


def field_options(root: Any, record_selector: str, *, limit: int = 40) -> "list[IndexedElement]":
    """The extractable FIELD leaves inside the FIRST instance of ``record_selector`` as numbered
    options -- what a query agent picks its extract columns from. Each ``selector`` is scoped to
    the record subtree (``durable_selector(..., within=record)``), so it evaluates per row."""
    records = root.cssselect(record_selector)
    if not records:
        return []
    first = records[0]
    out: list[IndexedElement] = []
    for leaf in first.iter():
        if not isinstance(getattr(leaf, "tag", None), str):
            continue
        txt = text_of(leaf)
        if not txt or any(isinstance(c.tag, str) and text_of(c) for c in leaf):
            continue  # only leaves whose text is their own (not a container's aggregated text)
        out.append(IndexedElement(
            index=len(out) + 1, role=role_of(leaf, False), name=txt[:60],
            kind="content", selector=durable_selector(leaf, within=first),
        ))
        if len(out) >= limit:
            break
    return out


def render_table(rows: "list[IndexedElement]") -> str:
    """The numbered element table an agent reads: one ``N  role "name"`` line per element (with a
    ``(repeats ×K)`` tag on a repeated row). No selectors, no classes -- indexes in, and the loop
    resolves them to selectors."""
    lines: list[str] = []
    for e in rows:
        tail = f'  (repeats ×{e.repeats})' if e.repeats > 1 else ""
        name = f' "{e.name}"' if e.name else ""
        lines.append(f"{e.index}  {e.role}{name}{tail}")
    return "\n".join(lines)
