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

from web.fetch import Snapshot
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


def consent_wall(doc: Document) -> "Signal | None":
    """A cookie / GDPR consent gate: a consent banner is present and may overlay the content until
    dismissed (the remedy is an interaction, not a transport change)."""
    if doc.kind != "html":
        return None
    hit = doc.select("#onetrust-banner-sdk, #cookie-consent, [id*=cookie-banner], "
                     "[class*=cookie-consent], [aria-label*=consent], [data-consent]")
    if hit is not None:
        return Signal(name="consent_wall", confidence=0.6)
    return None


def infinite_scroll(doc: Document) -> "Signal | None":
    """The list grows on scroll rather than via a pager: a scroll sentinel / infinite-scroll hook
    is present (the remedy is a scroll loop on a live page, not a URL walk)."""
    if doc.kind != "html":
        return None
    if doc.select("[data-infinite-scroll], .infinite-scroll, [data-infinite], "
                  "[class*=infinite-scroll], .load-more[data-scroll]") is not None:
        return Signal(name="infinite_scroll", confidence=0.6)
    return None


def empty(doc: Document) -> "Signal | None":
    """A near-empty document: almost no readable text (a failed render, a blank shell, or a body
    that never populated). Distinct from ``spa`` -- this is 'nothing here', regardless of scripts."""
    if doc.kind != "html":
        return None
    body = doc.select("body")
    chars = len(body.text) if body is not None else 0
    if chars < 50:
        return Signal(name="empty", confidence=0.7, detail={"visible_chars": chars})
    return None


def blocked_status(snap: Snapshot) -> "Signal | None":
    """A transport-level block by STATUS -- 401/403 (denied), 429 (rate-limited). A Snapshot fact,
    not content: the remedy differs (backoff for 429; a stronger tier/proxy for 403)."""
    if snap.status in (401, 403):
        return Signal(name="blocked_status", confidence=0.9, detail={"status": snap.status})
    if snap.status == 429:
        return Signal(name="blocked_status", confidence=0.8, detail={"status": 429, "rate_limited": True})
    return None


def server_error(snap: Snapshot) -> "Signal | None":
    """A 5xx or a transport failure -- transient, worth a retry rather than a tier climb."""
    if snap.error is not None or 500 <= snap.status < 600:
        return Signal(name="server_error", confidence=0.9,
                      detail={"status": snap.status, "error": snap.error.code if snap.error else None})
    return None


__all__ = ["Signal", "spa", "login_wall", "pagination", "anti_bot",
           "consent_wall", "infinite_scroll", "empty", "blocked_status", "server_error"]
