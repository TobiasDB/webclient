"""Interactivity detection: which elements are click / hover / scroll targets.

System B of the stamp-stream substrate (see webclient/core/document/correlate.py for the
sibling correlation system). Deliberately SILOED: this module only *decides* whether an
element is interactive; wiring the results into the capture stream and the skeleton is a
separate integration.

Two detector tiers, OR'd (a robust "depth" story):
  * STATIC / semantic (here, pure -- runs on the parsed tree): native interactive tags,
    ARIA roles, ``onclick`` / ``tabindex`` / ``contenteditable``.
  * DYNAMIC (a later seam, needs the browser): ``addEventListener`` wrapping (direct
    listeners) + ``cursor: pointer`` computed style (catches EVENT DELEGATION, e.g. React
    attaching one listener at the root) + scrollable-overflow containers. These arrive as
    per-node stamps and are merged via :func:`merge` -- the static tier already covers the
    common cases on its own.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

#: native tags that are click targets on their own (``a`` is handled separately -- only
#: interactive with an ``href``).
_CLICK_TAGS = frozenset({"button", "summary", "label", "option"})
#: form fields -- focus / edit / toggle targets (still "interactive" for the outline).
_FIELD_TAGS = frozenset({"input", "select", "textarea"})
#: ARIA roles that declare an interactive control even on a plain ``<div>``.
_CLICK_ROLES = frozenset({
    "button", "link", "tab", "menuitem", "menuitemcheckbox", "menuitemradio",
    "checkbox", "radio", "switch", "option", "combobox", "slider", "spinbutton",
})


class DomInteractive(BaseModel):
    """Whether -- and how -- an element is interactive."""

    node_key: str = ""  # the data-wc-node id when known (dynamic path); "" for a pure-static hit
    click: bool = False
    hover: bool = False
    scroll: bool = False
    source: str = ""  # how we know: "semantic" | "listener" | "cursor" | "scrollable"

    def __bool__(self) -> bool:
        return self.click or self.hover or self.scroll

    def merged(self, other: "DomInteractive") -> "DomInteractive":
        """OR two findings for the same node (e.g. static ``semantic`` + dynamic ``cursor``)."""
        srcs = [s for s in (self.source, other.source) if s]
        return DomInteractive(
            node_key=self.node_key or other.node_key,
            click=self.click or other.click,
            hover=self.hover or other.hover,
            scroll=self.scroll or other.scroll,
            source="+".join(dict.fromkeys(srcs)),  # ordered-unique
        )


def _tag(el: Any) -> str:
    return el.tag.lower() if isinstance(getattr(el, "tag", None), str) else ""


def interactive(el: Any) -> "DomInteractive | None":
    """The STATIC/semantic interactivity of one element, or ``None`` if it is not an
    interactive control by any semantic signal. Cheap and pure -- no browser, no capture."""
    tag = _tag(el)
    get = el.get if hasattr(el, "get") else (lambda _k: None)
    role = (get("role") or "").strip().lower()

    click = (
        (tag == "a" and get("href") is not None)
        or tag in _CLICK_TAGS
        or tag in _FIELD_TAGS
        or role in _CLICK_ROLES
        or get("onclick") is not None
        or get("tabindex") is not None
        or get("contenteditable") is not None
    )
    if not click:
        return None
    return DomInteractive(node_key=get("data-wc-node") or "", click=True, source="semantic")


def merge(
    static: "dict[str, DomInteractive]", dynamic: "dict[str, DomInteractive]"
) -> "dict[str, DomInteractive]":
    """Combine the static findings with dynamic (capture-time) ones, keyed by node id --
    the seam the future ``addEventListener`` / ``cursor:pointer`` / scroll stamps plug into."""
    out: dict[str, DomInteractive] = dict(static)
    for key, dyn in dynamic.items():
        out[key] = out[key].merged(dyn) if key in out else dyn
    return out


__all__ = ["DomInteractive", "interactive", "merge"]
