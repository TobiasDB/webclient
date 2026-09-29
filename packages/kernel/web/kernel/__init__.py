"""web.kernel -- the bottom layer.

The one package every other layer may import and which imports none of them (only pydantic).
It holds the shared substrate: structured :mod:`.errors` (``WebError`` / ``WebException``) and
the :mod:`.events` bus (the ``Event`` Protocol + ``EventBus``; each layer defines its own event
types). Nothing domain-specific lives here -- no Request, Snapshot, or Document; those belong
to the layers that own them.
"""

from __future__ import annotations

from .errors import WebError, WebException, err
from .events import Event, EventBus, Subscription, Trace, emit, topic_matches, using

__all__ = [
    "WebError",
    "WebException",
    "err",
    "Event",
    "EventBus",
    "Subscription",
    "topic_matches",
    "emit",
    "using",
    "Trace",
]
