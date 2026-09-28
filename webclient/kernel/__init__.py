"""webclient.kernel -- the bottom layer: pure data + the event bus, nothing above.

The kernel is the one package every other layer may import and which imports none
of them. It holds the cross-cutting value models (the ``Event`` taxonomy, the
structured ``WebError``), the event bus, rrweb reconstruction types, and logging
config. It depends only on the standard library and pydantic.

Isolation is enforced by ``tests/test_kernel_isolation.py``: no kernel module may
import from a sibling webclient layer (core / query / clients / pipelines / ...).
Keep it that way -- a feature that needs a client or a document does not belong here.

The flat modules that used to live at ``webclient.errors`` / ``webclient.models`` /
``webclient.events`` / ``webclient.rrweb`` / ``webclient.log`` now live here; the old
top-level names remain as thin alias shims (``sys.modules`` re-binding) so existing
imports keep working while call sites migrate to ``webclient.kernel.*``.
"""

from __future__ import annotations

from .errors import WebError, WebException, make
from .events import EventBus, Subscription
from .models import (
    ErrorEvent,
    Event,
    LoopEvent,
    PlanEvent,
)

__all__ = [
    "WebError",
    "WebException",
    "make",
    "EventBus",
    "Subscription",
    "Event",
    "ErrorEvent",
    "LoopEvent",
    "PlanEvent",
]
