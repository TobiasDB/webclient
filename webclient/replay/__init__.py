"""The STATIC replay engine (roadmap N3): rebuild what a run saw from its trace, with no
network and no browser -- ``replay="static"`` in the roadmap's three modes:

* **static** (this module) -- documents and the event timeline are PROJECTIONS of the
  trace's events (every :class:`~webclient.models.SnapshotEvent` becomes an offline
  ``Document`` carrying its captured content, headers, tiers and the events routed to it),
  so every read-only op -- ``select`` / ``attr`` / ``markdown`` / ``skeleton`` / ``flags``
  / ``card`` / ``events_of`` -- answers exactly as it did live. Any IO op raises
  ``replay.offline``: a projection never triggers a side effect.
* **live** -- re-execute the recorded Plan against the live web (``rec.plan.collect()``).
* **har** -- re-execute the Plan with the network served from the trace's HAR:
  ``WebClient(har=trace.har_path)`` for the static tier (see :mod:`.har`),
  ``BrowserConfig(replay_har=...)`` for the browser tier (Playwright's ``route_from_har``).

    rep = Replay("run.trace")
    for doc in rep.documents():           # offline Documents, in capture order
        print(doc.url, doc.title, doc.flags())
    rep.timeline()                        # every event, typed and ordered
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..models import Event, SnapshotEvent
from ..trace import TraceReader, read

if TYPE_CHECKING:
    from ..core.document import Document

__all__ = ["Replay", "offline_client", "document_from_snapshot"]


def offline_client() -> Any:
    """A ``WebClient`` with NO transport: it has a loop and a bus (so in-memory ops that
    bridge through the client still work) but every fetch/resolve raises ``replay.offline``.
    The client every replayed document is bound to."""
    from ..core.client import WebClient
    from ..core.engine import Engine

    wc = WebClient()
    old = wc._engine
    if old is not None:
        old.close()
    wc._engine = Engine(wc.browser_config, transport=False)
    return wc


def document_from_snapshot(
    snap: SnapshotEvent, events: "list[Event]", *, client: Any = None
) -> "Document":
    """Project one snapshot (+ the events routed to its document) into an offline
    ``Document``."""
    from ..core.document import Document

    doc = Document(
        url=snap.url, final_url=snap.final_url or snap.url, kind=snap.kind,  # type: ignore[arg-type]
        status_code=snap.status_code, content=snap.content or b"", response_headers=dict(snap.headers),
        encoding=snap.encoding,
    )
    doc.id = doc.name = snap.document_id or ""
    doc._client = client
    doc._tiers = list(snap.tiers)
    doc._events = [e for e in events if e.document_id and e.document_id == snap.document_id
                   and not isinstance(e, SnapshotEvent)]
    return doc


class Replay:
    """The static projection of a trace: offline documents, the timeline, and the errors."""

    def __init__(self, path: "str | Path | TraceReader") -> None:
        self.reader = path if isinstance(path, TraceReader) else read(path)
        self.client = offline_client()

    @property
    def path(self) -> Path:
        return self.reader.path

    def timeline(self) -> "list[Event]":
        """Every event in the trace, typed, in publish order."""
        return self.reader.events

    def documents(self, *, phase: "str | None" = None) -> "list[Document]":
        """Offline documents, one per snapshot (``phase`` narrows to ``fetch`` / ``load`` /
        ``action`` snapshots). Later snapshots of the same document (after an interaction)
        are separate entries, so the sequence of states is preserved."""
        events = self.reader.events
        return [
            document_from_snapshot(s, events, client=self.client)
            for s in self.reader.snapshots
            if phase is None or s.phase == phase
        ]

    def document(self, name: str, *, last: bool = True) -> "Document | None":
        """The document named ``name`` at its last (or first) snapshot, or ``None``."""
        found = [d for d in self.documents() if d.name == name]
        if not found:
            return None
        return found[-1] if last else found[0]

    def errors(self) -> "list[Any]":
        """The trace's ErrorEvents -- the ledger as recorded."""
        return self.reader.of("error")

    @property
    def har_path(self) -> "Path | None":
        """The merged HAR for ``"har"`` replay, written next to the trace on first use."""
        merged = self.reader.har()
        if not merged["log"]["entries"]:
            return None
        from .har import save_har

        target = self.path / "replay.har"
        if not target.exists():
            save_har(merged, target)
        return target

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "Replay":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
