"""Resiliency: response classification (pure) + the escalation ladder that acts on
the ``Resolve`` policies. Detection is pure functions (no cores, no IO) so a local
and a remote resolve agree on what to escalate. See docs/design/resiliency.md."""

from __future__ import annotations

from .detect import build_flag, static_flags, static_signals
from .headers import policy_headers

__all__ = ["static_signals", "static_flags", "build_flag", "policy_headers"]
