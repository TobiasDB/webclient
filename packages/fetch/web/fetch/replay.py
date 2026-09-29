"""Recording and replay -- two more BACKENDS of the fetch layer.

They implement the same ``fetch(request) -> Snapshot`` protocol as HTTP and Browser, so to
everything above they are interchangeable. Fetch has no idea whether it is live or replayed --
and no idea about tiers/ladders (that is resolve's concern).

:class:`Recorder` wraps another backend and records each response as a :class:`NetworkEvent`
(with its body) on the bus, so a :class:`~web.fetch.Trace` collects a replayable stream.
:class:`ReplayBackend` answers each Request from such a stream -- offline and deterministic; a
request with no recorded match returns a visible DRIFT Snapshot (status 599, ``replay.miss``), so
a replay that diverges from its recording is obvious rather than silent.
"""

from __future__ import annotations

from .bus import emit
from .errors import err

from .base import Fetcher
from .events import NetworkEvent
from .request import Request
from .snapshot import Snapshot


class Recorder:
    """A backend that wraps ``inner`` and records each response (with body) as a NetworkEvent on
    the bus. Opt-in: without a Recorder no bodies are captured, so live fetching stays light."""

    def __init__(self, inner: Fetcher) -> None:
        self._inner = inner

    async def fetch(self, request: Request) -> Snapshot:
        snap = await self._inner.fetch(request)
        emit(NetworkEvent(method=request.method.upper(), url=request.url,
                          status=snap.status, body=snap.content, source="recorder"))
        return snap

    async def aclose(self) -> None:
        await self._inner.aclose()


class ReplayBackend:
    """A backend that answers each Request from a recorded stream of :class:`NetworkEvent`s.
    Offline and deterministic; a miss is a visible drift Snapshot, never a real fetch."""

    def __init__(self, events: "list[NetworkEvent]") -> None:
        self._by_key: dict[tuple[str, str], NetworkEvent] = {}
        for e in events:
            if isinstance(e, NetworkEvent):
                self._by_key.setdefault((e.method.upper(), e.url), e)

    async def fetch(self, request: Request) -> Snapshot:
        e = self._by_key.get((request.method.upper(), request.url))
        if e is None:  # drift -> a visible miss, not a network call
            return Snapshot(
                request=request, url=request.url, status=599,
                error=err("replay.miss", f"no recording for {request.method.upper()} {request.url}", url=request.url),
            )
        return Snapshot(request=request, url=e.url, status=e.status, content=e.body)

    async def aclose(self) -> None:
        pass


__all__ = ["Recorder", "ReplayBackend"]
