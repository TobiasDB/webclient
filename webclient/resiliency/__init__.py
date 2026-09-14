"""Resiliency: response classification (pure) + the escalation ladder that acts on
the ``Resolve`` policies. Detection is pure functions (no cores, no IO) so a local
and a remote resolve agree on what to escalate. See docs/design/resiliency.md."""

from __future__ import annotations

from .detect import Signals, classify

__all__ = ["Signals", "classify"]
