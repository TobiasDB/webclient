"""The replay interface over ONE stream (roadmap N3, revised 2026-09-24): every replay is a
projection or a translation of the trace's events, never a second record.

* :meth:`Replay.state` -- the unified cursor: everything the run knew at event ``n`` (the
  documents at their latest snapshot, the network, console, actions, loops, pipelines,
  errors so far) -- what a UI pane shows when the scrubber sits at ``n``.
* :meth:`Replay.rrweb` -- the same stream as one rrweb event list (DOM + every other event as
  a custom event) for the DOM player; :mod:`.rrweb` translates both ways.
* :meth:`Replay.har` / :attr:`Replay.har_path` -- the network as a HAR, derived on demand.
* :meth:`Replay.plan` -- the Plan that produced the run, to re-execute live or over the HAR.

``replay="static"`` in the roadmap's three modes:

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

    rep = Replay("run.jsonl")
    for doc in rep.documents():           # offline Documents, in capture order
        print(doc.url, doc.title, doc.flags())
    rep.timeline()                        # every event, typed and ordered
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..models import Event, SnapshotEvent
from ..trace import TraceReader, read

if TYPE_CHECKING:
    from ..core.document import Document

__all__ = ["Replay", "ReplayState", "offline_client", "document_from_snapshot"]


@dataclass
class ReplayState:
    """Everything the run knew at a cursor: one object every replay pane reads from."""

    n: "int | None"
    events: "list[Event]"
    documents: "list[Document]"
    network: "list[Event]"
    console: "list[Event]"
    actions: "list[Event]"
    loops: "list[Event]"
    pipelines: "list[Event]"
    errors: "list[Event]"
    scripts: "list[Event]"

    def document(self, name: str) -> "Document | None":
        return next((d for d in self.documents if d.name == name), None)


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

    def har(self) -> dict[str, Any]:
        """The trace's network as a HAR ``log`` (derived from the stream)."""
        return self.reader.har()

    @property
    def har_path(self) -> "Path | None":
        """A HAR FILE for the replayers that need one (``WebClient(har=)``,
        ``BrowserConfig(replay_har=)``) -- derived from the stream into a cache next to the
        system temp dir (never stored in the trace), refreshed when the trace is newer."""
        import hashlib
        import tempfile

        merged = self.har()
        if not merged["log"]["entries"]:
            return None
        from .har import save_har

        key = hashlib.sha1(str(self.path.resolve()).encode()).hexdigest()[:16]
        target = Path(tempfile.gettempdir()) / "webclient-har" / f"{key}.har"
        if not target.exists() or target.stat().st_mtime < self.path.stat().st_mtime:
            save_har(merged, target)
        return target

    def rrweb(self, document_id: "str | None" = None, *, custom: bool = True) -> "list[dict[str, Any]]":
        """The stream as rrweb events for the DOM player (see :mod:`.rrweb`)."""
        return self.reader.rrweb(document_id, custom=custom)

    def plan(self, client: Any = None) -> Any:
        """The Plan that produced the run (``None`` when none was recorded), bound to
        ``client`` so ``.collect()`` re-executes it -- live, or over the HAR with
        ``WebClient(har=rep.har_path)``."""
        blob = self.reader.plan_blob
        if blob is None:
            return None
        from ..query.expr import from_blob

        return from_blob(blob, client)

    def state(self, n: "int | None" = None) -> "ReplayState":
        """The unified cursor: what the run knew after event ``n`` (default: everything)."""
        events = [e for e in self.reader.events if n is None or (e.n or 0) <= n]
        latest: dict[str, SnapshotEvent] = {}
        for e in events:
            if isinstance(e, SnapshotEvent) and e.document_id:
                latest[e.document_id] = e
        docs = [document_from_snapshot(s, events, client=self.client) for s in latest.values()]
        def by(t: str) -> "list[Event]":
            return [e for e in events if e.topic == t or e.topic.startswith(t + ".")]

        return ReplayState(n=n, events=events, documents=docs, network=by("network"), console=by("console"),
                           actions=by("action"), loops=by("loop"), pipelines=by("pipeline"),
                           errors=by("error"), scripts=by("script"))

    def at(self, ts: float) -> "ReplayState":
        """The state at an absolute timestamp (seconds)."""
        n = max((e.n or 0) for e in self.reader.events if (e.ts or 0) <= ts) if self.reader.events else None
        return self.state(n)

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "Replay":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
