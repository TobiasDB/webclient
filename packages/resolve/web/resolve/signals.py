"""Signals: purely-functional detectors over a parsed :class:`~web.parse.Document`.

Signals live in resolve, ABOVE parse, so parse stays strictly ``bytes -> Document`` (structure
only, no interpretation of intent). A detector is a pure function ``Document -> Signal | None``
-- no state, no IO -- and :func:`detect` runs them all. The resolve layer uses signals to make
policy decisions (an ``spa`` signal drives browser escalation; a ``login_wall`` has no transport
remedy), so detection and policy are separable and each detector is trivially testable.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from web.parse import Document


class Signal(BaseModel):
    """A named piece of evidence detected on a document, with a confidence and free detail."""

    name: str
    confidence: float = 1.0
    detail: dict[str, Any] = {}


def spa(doc: Document) -> "Signal | None":
    """A client-rendered shell: a mount node and scripts but little server-rendered text --
    the content arrives via JS, so a static fetch sees an empty page (escalate to a browser)."""
    if doc.kind != "html":
        return None
    body = doc.select("body")
    visible = len(body[0].text) if body else 0
    mounts = doc.select("#root, #app, [data-reactroot], [data-server-rendered], [ng-version]")
    if visible < 200 and mounts and doc.select("script[src]"):
        return Signal(name="spa", confidence=0.8, detail={"visible_chars": visible})
    return None


def login_wall(doc: Document) -> "Signal | None":
    """A login gate: a password field is present, so the content is behind auth (no transport
    remedy -- the caller must supply credentials/session)."""
    if doc.kind == "html" and doc.select("input[type=password]"):
        return Signal(name="login_wall")
    return None


def pagination(doc: Document) -> "Signal | None":
    """The document is one page of many: a rel=next or a pagination control is present."""
    if doc.kind == "html" and doc.select("a[rel~=next], link[rel=next], .pagination a, nav.pager a, [aria-label=Next]"):
        return Signal(name="pagination")
    return None


#: the built-in detectors, run in order by :func:`detect`.
DETECTORS: tuple[Callable[[Document], "Signal | None"], ...] = (spa, login_wall, pagination)


def detect(doc: Document) -> list[Signal]:
    """Run every detector and return the signals that fired (pure -- no IO, no state)."""
    return [s for d in DETECTORS if (s := d(doc)) is not None]


__all__ = ["Signal", "detect", "spa", "login_wall", "pagination", "DETECTORS"]
