"""ElementIndexBacking: the indexed element table -- a DOM-addressing primitive.

The shared foundation the index-based agent loops (Phase 4 interaction, Phase 5 query) build
on: an agent sees a NUMBERED table of elements (index + role + name, never a class) and returns
an index; we resolve that index to a DURABLE, class-free CSS selector for the recorded Plan. So
the model reasons in indexes and never authors (or hallucinates) a selector, while the Plan it
produces still serialises and replays against a fresh render.

``controls()`` gives the INTERACTIVE elements (what to click / type / wait for); ``content()``
gives the text-bearing / repeated-record elements (what to extract), with ``repeats`` marking a
member of a repeated structure so a query agent can choose ``select_all`` knowingly. Both are
built from the parsed tree via :func:`index_elements`, so they work on a static resolved page,
not only a live one. :func:`durable_selector` is the resolver: id / name / aria-label / a stable
semantic class / a structural nth-of-type path -- never a volatile utility or hashed class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from ...dom import tag, text_of
from ..web_core import Backing
from .html import _is_noise_class, tree
from .interactivity import interactive
from .models import IndexedElement
from .naming import _wordlike, name as _name
from .record_regions import _path, _scan

if TYPE_CHECKING:
    from . import Document

#: coarse control roles by tag, for the table's role column (a11y-flavoured but tag-derived).
_ROLE_BY_TAG = {
    "a": "link", "button": "button", "select": "combobox", "textarea": "textbox",
    "summary": "disclosure", "label": "label", "option": "option",
}
#: input ``type`` -> role (a text-ish field is a textbox; the rest keep their type as the role).
_TEXTISH = frozenset({"text", "search", "email", "url", "tel", "password", "number", ""})


def _role(el: Any, is_interactive: bool) -> str:
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
    if eid and _wordlike(eid) and not _is_noise_class(eid):
        return f"#{eid}"
    t = tag(el)
    nm = el.get("name")
    if nm:
        return f'{t}[name="{_esc_attr(nm)}"]'
    aria = el.get("aria-label")
    if aria and '"' not in aria:
        return f'{t}[aria-label="{_esc_attr(aria)}"]'
    stable = [c for c in (el.get("class") or "").split() if not _is_noise_class(c)]
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
                index=len(out) + 1, role=_role(el, True),
                name=label.label if label else text_of(el)[:60],
                kind="interactive", selector=durable_selector(el),
            ))
        else:  # content: text-bearing leaves (skip pure containers and empty nodes)
            txt = text_of(el)
            if not txt or any(isinstance(c.tag, str) and text_of(c) for c in el):
                continue  # a container whose text comes from child elements -> not a leaf
            out.append(IndexedElement(
                index=len(out) + 1, role=_role(el, di is not None),
                name=txt[:80], kind="content", selector=durable_selector(el),
                repeats=repeats.get(_path(el), 1),
            ))
        if len(out) >= limit:
            break
    return out


def _repeat_counts(root: Any) -> "dict[str, int]":
    """Map each record MEMBER's canonical xpath -> the size of its repeated group, so a content
    element inside a repeated row is marked ``repeats=N`` (built from the record-region scan)."""
    counts: dict[str, int] = {}
    for container, region in _scan(root, min_items=3):
        for member in container:
            if isinstance(getattr(member, "tag", None), str):
                for node in member.iter():
                    p = _path(node)
                    if p:
                        counts[p] = max(counts.get(p, 1), region.count)
    return counts


def _render_table(rows: "list[IndexedElement]") -> str:
    """The numbered element table an agent reads: one ``N  role "name"`` line per element (with a
    ``(repeats ×K)`` tag on a repeated row). No selectors, no classes -- indexes in, and the loop
    resolves them to selectors."""
    lines: list[str] = []
    for e in rows:
        tail = f'  (repeats ×{e.repeats})' if e.repeats > 1 else ""
        name = f' "{e.name}"' if e.name else ""
        lines.append(f"{e.index}  {e.role}{name}{tail}")
    return "\n".join(lines)


class ElementIndexBacking(Backing):
    """The indexed element table facet: ``controls()`` (interactive elements) /
    ``content_elements()`` (text/record elements) as :class:`IndexedElement` lists, and
    ``element_table()`` rendering
    the numbered view an agent picks indexes from. html/xml only."""

    provides = frozenset({"controls", "content_elements", "element_table"})
    gate = "ok"

    def applies(self, core: "Document") -> bool:
        """In play for markup documents (html/xml) -- the only kinds with an addressable DOM."""
        return core.kind in ("html", "xml")

    def controls(self, core: "Document") -> "list[IndexedElement]":
        """The INTERACTIVE elements (buttons / fields / links / …) as a numbered, class-free
        table -- what an interaction agent picks a target from; each carries a durable selector."""
        return index_elements(tree(core), kind="interactive")

    def content_elements(self, core: "Document") -> "list[IndexedElement]":
        """The text-bearing / repeated-record elements as a numbered table -- what a query agent
        picks a record + fields from; ``repeats`` marks a member of a repeated row."""
        return index_elements(tree(core), kind="content")

    def element_table(self, core: "Document", *, interactive: bool = True) -> str:
        """The numbered element table as text (interactive controls by default, else content) --
        the token-lean view an agent reasons over, returning indexes we resolve to selectors."""
        rows = self.controls(core) if interactive else self.content_elements(core)
        return _render_table(rows)


__all__ = ["ElementIndexBacking", "durable_selector", "index_elements"]
