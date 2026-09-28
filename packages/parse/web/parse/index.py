"""The indexed element table -- a DOM-addressing primitive.

An agent sees a NUMBERED table of elements (index + role + name, never a class) and returns an
index; :func:`durable_selector` resolves that index to a class-free CSS selector for the recorded
plan. So the model reasons in indexes and never authors (or hallucinates) a selector, while the
plan still replays against a fresh render. ``kind="interactive"`` lists the controls to drive;
``kind="content"`` lists text-bearing leaves, with ``repeats`` marking a member of a record row.
Pure and static -- the browser accessibility tree would be a higher-fidelity role/name source and
is a natural future alternate (same :class:`IndexedElement` shape).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from .classes import is_noise_class
from .nodes import Node, tag as _tag, text as _text

if TYPE_CHECKING:
    from .document import Document

# -- interactivity: is this element a control? (static/semantic tier) --
_CLICK_TAGS = frozenset({"button", "summary", "label", "option"})
_FIELD_TAGS = frozenset({"input", "select", "textarea"})
_CLICK_ROLES = frozenset({"button", "link", "tab", "menuitem", "menuitemcheckbox", "menuitemradio",
                          "checkbox", "radio", "switch", "option", "combobox", "slider", "spinbutton"})
_ROLE_BY_TAG = {"a": "link", "button": "button", "select": "combobox", "textarea": "textbox",
                "summary": "disclosure", "label": "label", "option": "option"}
_TEXTISH = frozenset({"text", "search", "email", "url", "tel", "password", "number", ""})
_VALUE_TYPES = frozenset({"submit", "button", "reset"})
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SEP = re.compile(r"[-_./]+")
_HASHISH = re.compile(r"^(?=.*\d)(?=.*[a-z])(?=.*[A-Z]).{5,}$|^[0-9a-f]{6,}$", re.I)


class IndexedElement(BaseModel):
    """One entry in the numbered table -- what an agent reasons over without seeing classes.
    ``selector`` is the durable, class-free CSS the index resolves to; ``repeats`` > 1 marks a
    member of a repeated structure (a record row), so a query agent can pick ``select_all``."""

    index: int
    role: str = ""
    name: str = ""
    kind: Literal["interactive", "content"] = "interactive"
    selector: str = ""
    repeats: int = 1


def interactive(el: Node) -> bool:
    """Whether ``el`` is a control by any static signal -- native tag, ARIA role, ``onclick`` /
    ``tabindex`` / ``contenteditable``."""
    tag, get = _tag(el), el.get
    return bool(
        (tag == "a" and get("href") is not None) or tag in _CLICK_TAGS or tag in _FIELD_TAGS
        or (get("role") or "").strip().lower() in _CLICK_ROLES
        or get("onclick") is not None or get("tabindex") is not None or get("contenteditable") is not None
    )


def role_of(el: Node, is_interactive: bool) -> str:
    """A coarse role for the table: explicit ARIA role, else a tag/input-type mapping, else
    ``control``/``text``."""
    aria = (el.get("role") or "").strip().lower()
    if aria:
        return aria
    if _tag(el) == "input":
        it = (el.get("type") or "text").strip().lower()
        return "textbox" if it in _TEXTISH else it
    return _ROLE_BY_TAG.get(_tag(el), "control" if is_interactive else "text")


def _wordlike(s: str) -> bool:
    s = s.strip()
    if len(s) < 2 or not any(c.isalpha() for c in s):
        return False
    return not any(_HASHISH.match(tok) for tok in _SEP.split(s) if tok)


def element_name(el: Node, *, max_len: int = 60) -> str:
    """A short human label for ``el`` -- aria-label / alt / title / placeholder / a control's value
    / short visible text / a word-like humanized id -- or ``""`` (a hashed id yields nothing)."""
    get = el.get
    for attr in ("aria-label", "alt", "title", "placeholder"):
        v = (get(attr) or "").strip()
        if v and _wordlike(v):
            return v[:max_len]
    tag = _tag(el)
    if tag == "button" or (tag == "input" and (get("type") or "").lower() in _VALUE_TYPES):
        v = (get("value") or "").strip()
        if v and _wordlike(v):
            return v[:max_len]
    txt = _text(el)
    if 0 < len(txt) <= max_len and _wordlike(txt):
        return txt
    for attr in ("id", "name"):
        v = get(attr) or ""
        if v and _wordlike(v):
            return _SEP.sub(" ", _CAMEL.sub(" ", v)).strip().lower()[:max_len]
    return ""


def _nth_step(el: Node) -> str:
    t = _tag(el)
    parent = el.getparent()
    if parent is None:
        return t
    same = [c for c in parent if _tag(c) == t]
    return f"{t}:nth-of-type({same.index(el) + 1})" if len(same) > 1 else t


def durable_selector(el: Node, *, within: "Node | None" = None) -> str:
    """A durable, class-free CSS selector for ``el`` that matches a fresh render: a word-like
    ``#id``, a ``[name]`` / ``[aria-label]``, one stable semantic class, else a structural
    ``nth-of-type`` path (scoped to ``within`` for a per-record field selector)."""
    eid = el.get("id")
    if eid and _wordlike(eid) and not is_noise_class(eid):
        return f"#{eid}"
    t = _tag(el)
    for attr in ("name", "aria-label"):
        v = el.get(attr)
        if v and '"' not in v:
            return f'{t}[{attr}="{v}"]'
    stable = [c for c in (el.get("class") or "").split() if not is_noise_class(c)]
    if stable:
        return f"{t}.{sorted(stable, key=len)[-1]}"
    steps: list[str] = []
    node: "Node | None" = el
    while node is not None and node is not within and _tag(node):
        steps.append(_nth_step(node))
        node = node.getparent()
    return " > ".join(reversed(steps))


def index_elements(doc: "Document", *, kind: "Literal['interactive', 'content']", limit: int = 200) -> "list[IndexedElement]":
    """The numbered element table for a markup document. ``interactive`` = the controls to drive;
    ``content`` = text-bearing leaves, each ``repeats``-marked from the detected record regions."""
    if not doc._markup():
        return []
    root = doc._root()
    repeats = _repeat_counts(doc) if kind == "content" else {}
    out: list[IndexedElement] = []
    for el in root.iter():
        if not _tag(el):
            continue
        if kind == "interactive":
            if not interactive(el):
                continue
            out.append(IndexedElement(index=len(out) + 1, role=role_of(el, True),
                                      name=element_name(el) or _text(el)[:60], kind="interactive",
                                      selector=durable_selector(el)))
        else:
            txt = _text(el)
            if not txt or any(_tag(c) and _text(c) for c in el):
                continue  # a container whose text comes from children -> not a leaf
            out.append(IndexedElement(index=len(out) + 1, role=role_of(el, interactive(el)),
                                      name=txt[:80], kind="content", selector=durable_selector(el),
                                      repeats=repeats.get(id(el), 1)))
        if len(out) >= limit:
            break
    return out


def _repeat_counts(doc: "Document") -> "dict[int, int]":
    """Map each node's id() -> the size of the record group it belongs to (from the record scan)."""
    counts: dict[int, int] = {}
    for region in doc.records(top_k=5):
        for member in doc.select_all(region.item_selector):
            for node in member._node.iter():
                counts[id(node)] = max(counts.get(id(node), 1), region.count)
    return counts


def record_options(doc: "Document", *, top_k: int = 5) -> "list[IndexedElement]":
    """The repeated-record regions as numbered options -- what a query agent picks its
    ``select_all`` container from. Each ``selector`` is the region's item selector."""
    out: list[IndexedElement] = []
    for region in doc.records(top_k=top_k):
        out.append(IndexedElement(index=len(out) + 1, role="row", name=f"{region.count} records",
                                  kind="content", selector=region.item_selector, repeats=region.count))
    return out


def field_options(doc: "Document", record_selector: str, *, limit: int = 40) -> "list[IndexedElement]":
    """The text-bearing fields WITHIN one record, as numbered options with selectors RELATIVE to the
    record (so each evaluates per row) -- what a query agent picks each column from."""
    first = doc.select(record_selector)
    if first is None:
        return []
    record = first._node
    out: list[IndexedElement] = []
    for el in record.iter():
        if not _tag(el) or el is record:
            continue
        txt = _text(el)
        if not txt or any(_tag(c) and _text(c) for c in el):
            continue
        out.append(IndexedElement(index=len(out) + 1, role=role_of(el, False), name=txt[:80],
                                  kind="content", selector=durable_selector(el, within=record)))
        if len(out) >= limit:
            break
    return out


def render_table(rows: "list[IndexedElement]") -> str:
    """The numbered table as text for an agent prompt -- index, role, name (never the selector)."""
    return "\n".join(f"[{r.index}] {r.role}: {r.name}" for r in rows)


__all__ = ["IndexedElement", "interactive", "role_of", "element_name", "durable_selector",
           "index_elements", "record_options", "field_options", "render_table"]
