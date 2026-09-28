"""Signals -- a sub-part of resolve: clean, standalone detector functions.

Each signal is a plain pure function that returns a :class:`Signal` (or ``None``) from what it
needs -- mostly a parsed :class:`~web.parse.Document`. They are NOT lumped behind one uniform
interface: a caller (e.g. :func:`~web.resolve.escalate`) just calls the ones it cares about.
Signals live in resolve, not in parse (which stays strictly bytes -> Document); resolve uses them
to make policy decisions (escalate on ``spa``; a ``login_wall`` has no transport remedy).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from web.parse import Document


class Signal(BaseModel):
    """A named piece of evidence, with a confidence and free detail."""

    name: str
    confidence: float = 1.0
    detail: dict[str, Any] = {}


def spa(doc: Document) -> "Signal | None":
    """A client-rendered shell: a mount node and scripts but little server-rendered text -- the
    content arrives via JS, so a static fetch sees an empty page (escalate to a browser)."""
    if doc.kind != "html":
        return None
    body = doc.select("body")
    visible = len(body.text) if body is not None else 0
    mounts = doc.select("#root, #app, [data-reactroot], [data-server-rendered], [ng-version]")
    if visible < 200 and mounts is not None and doc.select("script") is not None:
        return Signal(name="spa", confidence=0.8, detail={"visible_chars": visible})
    return None


def login_wall(doc: Document) -> "Signal | None":
    """A login gate: a password field is present, so the content is behind auth (no transport
    remedy -- the caller must supply credentials/session)."""
    if doc.kind == "html" and doc.select("input[type=password]") is not None:
        return Signal(name="login_wall")
    return None


def pagination(doc: Document) -> "Signal | None":
    """The document is one page of many: a rel=next or a pagination control is present."""
    if doc.kind == "html" and doc.select("a[rel~=next], link[rel=next], .pagination a, nav.pager a, [aria-label=Next]") is not None:
        return Signal(name="pagination")
    return None


def anti_bot(doc: Document) -> "Signal | None":
    """An anti-bot wall in the CONTENT -- a CAPTCHA / challenge interstitial. (A blocking STATUS
    is a transport fact, checked on the Snapshot, not here.)"""
    if doc.kind != "html":
        return None
    text = doc.text.lower()
    markers = ("captcha", "cf-challenge", "verify you are human", "unusual traffic",
               "access denied", "are you a robot", "checking your browser")
    if any(m in text for m in markers):
        return Signal(name="anti_bot", confidence=0.7)
    return None


__all__ = ["Signal", "spa", "login_wall", "pagination", "anti_bot"]
