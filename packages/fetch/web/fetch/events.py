"""The fetch layer's event types -- what a browser fetch captures onto a Snapshot.

Each is a PLAIN model with a ``topic`` field -- it does not inherit a kernel base; the bus routes
it structurally (see :class:`web.kernel.Event`, a Protocol). Fetch captures :class:`NetworkEvent`
for every response the page made and :class:`DOMEvent` for the DOM changes a page script recorded
(rrweb or the built-in recorder -- see :mod:`.script`); those two streams are enough to REPLAY a
fetch (network answers requests, DOM records reconstruct the page), so no separate HAR is needed.
:data:`CaptureEvent` is the union a Snapshot carries.
"""

from __future__ import annotations

from pydantic import BaseModel, JsonValue


class FetchEvent(BaseModel):
    """One fetch a backend performed: the URL, the resulting status, and how long it took. The
    backend that produced it is on ``source`` (e.g. ``"http"`` / ``"browser"``)."""

    topic: str = "fetch"
    url: str = ""
    status: int = 0
    elapsed: float = 0.0
    source: str = ""


class NetworkEvent(BaseModel):
    """One response seen during a fetch (method, URL, status, resource type). Carries the response
    ``body`` when recorded (see :class:`~web.fetch.Recorder`), which is what makes the network
    stream replayable -- no separate HAR needed. ``source`` names what recorded it."""

    topic: str = "network"
    method: str = ""
    url: str = ""
    status: int = 0
    resource_type: str = ""
    body: bytes = b""
    source: str = ""


class DOMEvent(BaseModel):
    """A chunk of DOM records drained from a page script -- the internal form the raw rrweb (or
    recorder) output is converted into. ``records`` is the recorder's own JSON; ``script`` names
    the recorder that produced it."""

    topic: str = "dom"
    script: str = ""
    records: list[dict[str, JsonValue]] = []


class ConsoleEvent(BaseModel):
    """One ``console.*`` message the page logged during a browser fetch (level + text). A cheap
    window into client-side errors / debug output that never reaches the DOM."""

    topic: str = "console"
    level: str = ""
    text: str = ""


#: the events a :class:`~web.fetch.Snapshot` carries (fetch-layer capture). A concrete union (not
#: the kernel Event Protocol) so it is a valid pydantic field type.
CaptureEvent = NetworkEvent | DOMEvent | ConsoleEvent

__all__ = ["FetchEvent", "NetworkEvent", "DOMEvent", "ConsoleEvent", "CaptureEvent"]
