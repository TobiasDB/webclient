"""Shared document helpers used by more than one backing: the ``Element`` block
model and the element-subcore factory (``_element``). Kept out of ``__init__`` so
the backing modules can import them without a backing<->``__init__`` cycle;
``_element`` imports ``DocumentCore`` lazily at call time (after the package has
finished loading)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from . import DocumentCore


class Element(BaseModel):
    """A typed content block -- the "elements" representation."""

    id: str = ""
    type: str = "text"
    text: str = ""
    parent_id: str | None = None
    metadata: dict[str, Any] = {}


def _element(parent: "DocumentCore", node: Any) -> "DocumentCore":
    """A selected element/value as a DocumentCore rooted at ``parent``. A
    ``None`` node means the selection missed -- a not-ok, empty sub-document."""
    from . import DocumentCore

    content = b""
    if node is not None and not isinstance(node, (str, int, float, bool, list, dict)):
        try:
            from lxml import html as _lh

            content = _lh.tostring(node)  # the element's own bytes
        except Exception:
            content = b""
    sub = DocumentCore(
        url=parent.url,
        final_url=parent.final_url,
        kind=parent.kind,
        status_code=parent.status_code,
        content=content,
    )
    sub.root = parent.name or parent.root
    sub._client = parent._client
    sub._element = node
    sub._missing = node is None
    sub._events = parent._events  # a static element shares the store
    return sub
