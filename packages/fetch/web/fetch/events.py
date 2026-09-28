"""The fetch layer's event types -- what a browser fetch captures onto a Snapshot.

Each layer defines its own events (the kernel only knows the ``Event`` base). Fetch captures
two: :class:`NetworkEvent` for every response the page made, and :class:`DOMEvent` for the DOM
changes recorded by a page script (rrweb or the built-in recorder -- see :mod:`.script`). These
two streams are enough to REPLAY a fetch later (the network answers requests, the DOM records
reconstruct the page), so no separate HAR is needed; replay itself is a later layer's job.
"""

from __future__ import annotations

from pydantic import JsonValue

from web.kernel import Event


class FetchEvent(Event):
    """One fetch a backend performed: the URL, the resulting status, and how long it took. The
    backend that produced it is on ``source`` (e.g. ``"http"`` / ``"browser"``)."""

    topic: str = "fetch"
    url: str = ""
    status: int = 0
    elapsed: float = 0.0


class NetworkEvent(Event):
    """One response seen during a fetch (method, URL, status, resource type). Carries the response
    ``body`` when recorded (see :class:`~web.fetch.Recorder`), which is what makes the network
    stream replayable -- no separate HAR needed."""

    topic: str = "network"
    method: str = ""
    url: str = ""
    status: int = 0
    resource_type: str = ""
    body: bytes = b""


class DOMEvent(Event):
    """A chunk of DOM records drained from a page script -- the internal form the raw rrweb (or
    recorder) output is converted into. ``records`` is the recorder's own JSON; ``script`` names
    the recorder that produced it."""

    topic: str = "dom"
    script: str = ""
    records: list[dict[str, JsonValue]] = []


class ConsoleEvent(Event):
    """One ``console.*`` message the page logged during a browser fetch (level + text). A cheap
    window into client-side errors / debug output that never reaches the DOM."""

    topic: str = "console"
    level: str = ""
    text: str = ""


__all__ = ["FetchEvent", "NetworkEvent", "DOMEvent", "ConsoleEvent"]
