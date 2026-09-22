"""Traces: the persisted event stream (roadmap N3) -- an append-only JSONL log of every
event on an engine's bus plus sidecar assets (document snapshots, network bodies, browser
HARs), in a directory laid out like Playwright's ``trace.zip``::

    <trace>/
      manifest.json        schema_version, started/finished, package version, counts
      events.jsonl         one Event per line (model_dump(mode="json")); bytes offloaded
      snapshots/<n>.html   SnapshotEvent content (the document at that moment)
      bodies/<n>.bin       NetworkEvent bodies captured by the static tier
      har/*.har            Playwright HARs, one per browser context opened while tracing
      static.har           a HAR built from the static tier's navigations (on close)

Write one with :meth:`WebClient.trace` (``with wc.trace("run.trace"): ...``) or attach a
:class:`Trace` to any bus; read it back with :func:`read` / :class:`TraceReader`, which
upcasts old event shapes through the :class:`~webclient.events.EventRegistry`. The static
replay engine (:mod:`webclient.replay`) is a projection over a reader.

Events are immutable and the log is the source of truth; the assets are an optimisation
of the same facts (the snapshot's bytes), never a second record.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from .events import EventRegistry
from .models import ErrorEvent, Event, NetworkEvent, SnapshotEvent

if TYPE_CHECKING:
    from .events import EventBus, Subscription

SCHEMA_VERSION = 1
log = logging.getLogger(__name__)

__all__ = ["Trace", "TraceReader", "read", "SCHEMA_VERSION"]


def _kind_ext(kind: str) -> str:
    return {"html": "html", "json": "json", "xml": "xml"}.get(kind, "bin")


class Trace:
    """A trace WRITER: subscribes to a bus and appends every event to ``events.jsonl``,
    offloading byte payloads to asset files. Use as a context manager (closing writes the
    manifest and the static HAR)."""

    def __init__(self, path: "str | Path", *, inline_bytes: int = 0) -> None:
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        (self.path / "snapshots").mkdir(exist_ok=True)
        (self.path / "bodies").mkdir(exist_ok=True)
        (self.path / "har").mkdir(exist_ok=True)
        self.inline_bytes = inline_bytes  # payloads up to this size stay inline in the JSONL
        self.started = time.time()
        self.count = 0
        self._sub: "Subscription | None" = None
        self._events = (self.path / "events.jsonl").open("a", encoding="utf-8")
        self._network: list[NetworkEvent] = []  # for the static HAR
        self._write_manifest(finished=None)

    # -- attaching ------------------------------------------------------------
    def attach(self, bus: "EventBus", *, since: int = 0, topic: str = "") -> "Trace":
        """Subscribe to ``bus`` (from now on; ``since`` replays the bus's retained history
        past that cursor first so a late attach still captures the run so far)."""
        for event in bus.since(since, topic=topic):
            self.write(event)
        self._sub = bus.subscribe(topic, self.write)
        return self

    def detach(self) -> None:
        if self._sub is not None:
            self._sub.cancel()
            self._sub = None

    # -- writing --------------------------------------------------------------
    def write(self, event: Event) -> None:
        """Append one event: bytes fields are offloaded to an asset (path recorded on the
        event's ``asset``) unless small enough to inline; everything else is the event's
        JSON dump."""
        data = event.model_dump(mode="python", exclude_none=True)
        n = event.n if event.n is not None else self.count + 1
        if isinstance(event, SnapshotEvent) and event.content is not None:
            if len(event.content) > self.inline_bytes:
                rel = f"snapshots/{n}.{_kind_ext(event.kind)}"
                (self.path / rel).write_bytes(event.content)
                data["asset"] = rel
                data.pop("content", None)
            else:
                data["content"] = event.content.decode("utf-8", "replace")
        elif isinstance(event, NetworkEvent):
            self._network.append(event)
            body = data.pop("body", None)
            if body is not None and len(body) > self.inline_bytes:
                rel = f"bodies/{n}.bin"
                (self.path / rel).write_bytes(body)
                data["asset"] = rel
            elif body is not None:
                data["body"] = body.decode("utf-8", "replace")
            req = data.pop("request", None)  # a Reference core -> its url + method
            if req is not None:
                data["url"] = str(getattr(event.request, "url", "") or "")
                try:
                    data["url"] = str(event.request.dispatch("url"))
                except Exception:  # noqa: BLE001 - an unbound reference
                    pass
                data.setdefault("method", getattr(event.request, "method", None))
        elif isinstance(event, ErrorEvent):
            data["error"] = event.error.model_dump(mode="json", exclude_none=True)
        try:
            line = json.dumps(data, default=_json_default, separators=(",", ":"))
        except TypeError:
            line = json.dumps(json.loads(event.model_dump_json()), separators=(",", ":"))
        self._events.write(line + "\n")
        self.count += 1

    def _write_manifest(self, finished: "float | None") -> None:
        from . import __version__

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "webclient": __version__,
            "started": self.started,
            "finished": finished,
            "events": self.count,
            "har": sorted(p.name for p in (self.path / "har").glob("*.har")),
        }
        (self.path / "manifest.json").write_text(json.dumps(manifest, indent=1))

    def close(self) -> None:
        """Detach, flush, write the static HAR (from the captured navigations) and the final
        manifest."""
        from .replay.har import har_from_events, save_har

        self.detach()
        self._events.flush()
        self._events.close()
        if self._network:
            har = har_from_events(self._network)
            if har["log"]["entries"]:
                save_har(har, self.path / "static.har")
        self._write_manifest(finished=time.time())
        log.info("trace closed: %d events -> %s", self.count, self.path)

    def __enter__(self) -> "Trace":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _json_default(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    return str(value)


class TraceReader:
    """A trace READER: the manifest, the typed events (upcast through the registry) with
    their offloaded assets re-inflated on demand, and the HARs it holds."""

    def __init__(self, path: "str | Path", *, registry: "EventRegistry | None" = None) -> None:
        self.path = Path(path)
        self.registry = registry or EventRegistry()
        self.manifest: dict[str, Any] = json.loads((self.path / "manifest.json").read_text())
        self._events: "list[Event] | None" = None

    @property
    def schema_version(self) -> int:
        return int(self.manifest.get("schema_version", 1))

    def raw(self) -> Iterator[dict[str, Any]]:
        """The event dicts as stored, in order."""
        with (self.path / "events.jsonl").open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    yield json.loads(line)

    def asset(self, rel: str) -> bytes:
        """The bytes of an offloaded asset (a snapshot / body) by its relative path."""
        return (self.path / rel).read_bytes()

    @property
    def events(self) -> "list[Event]":
        """Every event, typed (via the registry) and with byte payloads re-inflated from
        their assets. Cached after the first read."""
        if self._events is None:
            out: list[Event] = []
            for data in self.raw():
                asset = data.get("asset")
                if asset and data.get("topic") == "snapshot":
                    data["content"] = self.asset(asset)
                elif asset and str(data.get("topic", "")).startswith("network"):
                    data["body"] = self.asset(asset)
                if str(data.get("topic", "")).startswith("network"):
                    data.pop("request", None)  # the reference is not rebuilt; ``url`` is kept
                try:
                    out.append(self.registry.load(data))
                except Exception as exc:  # noqa: BLE001 - one bad line never hides the rest
                    log.warning("trace: skipping unreadable event #%s (%s)", data.get("n"), exc)
            self._events = out
        return self._events

    def of(self, topic: str) -> "list[Event]":
        """The events whose topic matches ``topic`` by dotted prefix."""
        from .models import topic_matches

        return [e for e in self.events if topic_matches(topic, e.topic)]

    @property
    def snapshots(self) -> "list[SnapshotEvent]":
        return [e for e in self.events if isinstance(e, SnapshotEvent)]

    @property
    def har_files(self) -> "list[Path]":
        """The HAR files this trace holds: Playwright's per-context HARs and the static HAR."""
        files = sorted((self.path / "har").glob("*.har"))
        static = self.path / "static.har"
        return [*files, *([static] if static.exists() else [])]

    def har(self) -> dict[str, Any]:
        """One merged HAR ``log`` over every HAR file the trace holds (static first)."""
        from .replay.har import load_har

        entries: list[dict[str, Any]] = []
        for f in reversed(self.har_files):
            entries.extend(load_har(f).get("entries", []))
        return {"log": {"version": "1.2", "creator": {"name": "webclient"}, "entries": entries}}


def read(path: "str | Path") -> TraceReader:
    """Open a trace directory for reading."""
    return TraceReader(path)
