"""Traces: ONE event stream (roadmap N3, revised 2026-09-24).

A trace is a single append-only JSONL file -- every event on an engine's bus, one per line,
byte payloads included (a snapshot's content, a response body, an rrweb chunk), with a
``trace`` header on the first line and a ``trace`` footer on the last that carries the run's
counts and the PLAN that produced it. There are no sidecar artefacts: no manifest, no
snapshot files, no HARs, no rrweb chunk files. Everything a replay mechanism needs is an
event, so a new fact is a new event topic -- never a new artefact kind -- and every replay
(static projections, HAR replay, the rrweb DOM player, the UI) is a TRANSLATION of the same
stream (:mod:`webclient.replay`, :mod:`webclient.replay.har`, :mod:`webclient.replay.rrweb`).

    with WebClient() as wc, wc.trace("run.jsonl"):
        ...
    read("run.jsonl").events           # typed, in order, payloads inline
    Replay("run.jsonl").state(n)       # everything the run knew at event n

The log is the source of truth; old shapes are upcast on read through the
:class:`~webclient.events.EventRegistry`.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from .events import EventRegistry
from .models import Event, SnapshotEvent, TraceEvent

if TYPE_CHECKING:
    from .events import EventBus, Subscription

SCHEMA_VERSION = 2
log = logging.getLogger(__name__)

__all__ = ["Trace", "TraceReader", "read", "encode", "decode", "wire", "SCHEMA_VERSION"]

_PAYLOADS = ("content", "body", "events")  # the fields a wire view drops


def encode(event: Event, *, payload: bool = True) -> dict[str, Any]:
    """An event as a JSON-safe dict -- the ONE serialisation the trace, the socket and the
    rrweb custom events share. Bytes fields ride inline: UTF-8 text as-is, anything else
    base64 (named in ``_b64`` so :func:`decode` restores them). A network event's live
    ``request`` reference is rendered as its ``url`` + ``method``. ``payload=False`` drops
    the byte payloads and rrweb chunk bodies (the wire view: shape without weight)."""
    data = event.model_dump(mode="python", exclude_none=True)
    req = data.pop("request", None)
    if req is not None and not data.get("url"):
        try:
            data["url"] = str(event.request.dispatch("url"))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - an unbound reference: keep what it has
            data["url"] = str(getattr(req, "url", "") or "")
        data.setdefault("method", getattr(req, "method", None))
    err = getattr(event, "error", None)
    if err is not None and hasattr(err, "model_dump"):
        data["error"] = err.model_dump(mode="json", exclude_none=True)
    if not payload:
        for k in _PAYLOADS:
            if k in data:
                if k == "events":
                    data[k] = []
                else:
                    data.pop(k)
        return _jsonable(data)
    b64: list[str] = []
    for k, v in list(data.items()):
        if isinstance(v, (bytes, bytearray)):
            try:
                data[k] = bytes(v).decode("utf-8")
            except UnicodeDecodeError:
                data[k] = base64.b64encode(bytes(v)).decode("ascii")
                b64.append(k)
    if b64:
        data["_b64"] = b64
    return _jsonable(data)


def decode(data: dict[str, Any]) -> dict[str, Any]:
    """The inverse of :func:`encode` for the byte fields (the registry rebuilds the class)."""
    for k in data.pop("_b64", []) or []:
        if isinstance(data.get(k), str):
            data[k] = base64.b64decode(data[k])
    data.pop("request", None)
    return data


def wire(event: Event) -> dict[str, Any]:
    """The socket / API view of an event: :func:`encode` without payloads."""
    return encode(event, payload=False)


def _jsonable(data: dict[str, Any]) -> dict[str, Any]:
    try:
        json.dumps(data)
        return data
    except TypeError:
        return json.loads(json.dumps(data, default=_json_default))  # type: ignore[no-any-return]


def _json_default(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    return str(value)


class Trace:
    """A trace WRITER: subscribes to a bus and appends every event to one JSONL file. Use as
    a context manager; closing writes the footer (counts + the plan). ``plan`` is the Plan
    (an ``Expr`` / a blob string) that produced the run, set at open or any time before close
    (``WebClient.trace`` fills it from the client's recording session when there is one)."""

    def __init__(self, path: "str | Path", *, plan: Any = None) -> None:
        self.path = Path(path)
        if not self.path.suffix:
            self.path = self.path.with_suffix(".jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.plan = plan
        self.started = time.time()
        self.count = 0
        self._sub: "Subscription | None" = None
        self._fh = self.path.open("w", encoding="utf-8")
        from . import __version__

        self._line(TraceEvent(phase="start", ts=self.started, detail={
            "schema_version": SCHEMA_VERSION, "webclient": __version__, "started": self.started}))

    # -- attaching ------------------------------------------------------------
    def attach(self, bus: "EventBus", *, since: int = 0, topic: str = "", run_id: str | None = None) -> "Trace":
        """Subscribe to ``bus`` (from now on; ``since`` replays the bus's retained history
        past that cursor first so a late attach still captures the run so far). ``run_id`` keeps
        only that run's events (see ``events.run_scope``) -- another run on the same engine stays out."""
        for event in bus.since(since, topic=topic, run_id=run_id):
            self.write(event)
        self._sub = bus.subscribe(topic, self.write, run_id=run_id)
        return self

    def detach(self) -> None:
        if self._sub is not None:
            self._sub.cancel()
            self._sub = None

    # -- writing --------------------------------------------------------------
    def _line(self, event: Event) -> None:
        self._fh.write(json.dumps(encode(event), separators=(",", ":")) + "\n")

    def write(self, event: Event) -> None:
        """Append one event (payloads inline)."""
        self._line(event)
        self.count += 1

    def close(self) -> None:
        """Detach and write the footer: finished, the event count, the plan blob."""
        self.detach()
        plan = self.plan
        blob = plan if isinstance(plan, str) else (plan.to_blob() if hasattr(plan, "to_blob") else None)
        finished = time.time()
        self._line(TraceEvent(phase="end", ts=finished, detail={
            "finished": finished, "events": self.count, "plan": blob}))
        self._fh.flush()
        self._fh.close()
        log.info("trace closed: %d events -> %s", self.count, self.path)

    def __enter__(self) -> "Trace":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class TraceReader:
    """A trace READER: the typed events (upcast through the registry, payloads inline), the
    header / footer facts, and the translations every replay mechanism starts from."""

    def __init__(self, path: "str | Path", *, registry: "EventRegistry | None" = None) -> None:
        self.path = Path(path)
        self.registry = registry or EventRegistry()
        self._events: "list[Event] | None" = None

    def raw(self) -> Iterator[dict[str, Any]]:
        """The event dicts as stored, in order."""
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    yield json.loads(line)

    @property
    def events(self) -> "list[Event]":
        """Every event (the trace header / footer included), typed. Cached."""
        if self._events is None:
            out: list[Event] = []
            for data in self.raw():
                try:
                    out.append(self.registry.load(decode(data)))
                except Exception as exc:  # noqa: BLE001 - one bad line never hides the rest
                    log.warning("trace: skipping unreadable event #%s (%s)", data.get("n"), exc)
            self._events = out
        return self._events

    # -- header / footer ------------------------------------------------------
    @property
    def header(self) -> dict[str, Any]:
        first = next((e for e in self.events if isinstance(e, TraceEvent) and e.phase == "start"), None)
        return dict(first.detail) if first is not None else {}

    @property
    def footer(self) -> dict[str, Any]:
        last = next((e for e in reversed(self.events) if isinstance(e, TraceEvent) and e.phase == "end"), None)
        return dict(last.detail) if last is not None else {}

    @property
    def schema_version(self) -> int:
        return int(self.header.get("schema_version", 1))

    @property
    def count(self) -> int:
        """The run's events (footer count; a still-open trace counts its lines)."""
        n = self.footer.get("events")
        return int(n) if n is not None else sum(1 for e in self.events if not isinstance(e, TraceEvent))

    @property
    def plan_blob(self) -> "str | None":
        """The blob of the Plan that produced the run, when one was recorded."""
        return self.footer.get("plan")

    def summary(self) -> dict[str, Any]:
        """Header + footer facts in one dict (what ``/traces/{id}`` returns)."""
        return {**self.header, **{k: v for k, v in self.footer.items() if k != "plan"},
                "events": self.count, "snapshots": len(self.snapshots), "plan": self.plan_blob is not None,
                "documents": len({e.document_id for e in self.events if e.document_id})}

    # -- views ----------------------------------------------------------------
    def of(self, topic: str) -> "list[Event]":
        """The events whose topic matches ``topic`` by dotted prefix."""
        from .models import topic_matches

        return [e for e in self.events if topic_matches(topic, e.topic)]

    @property
    def snapshots(self) -> "list[SnapshotEvent]":
        return [e for e in self.events if isinstance(e, SnapshotEvent)]

    def rrweb(self, document_id: "str | None" = None, *, custom: bool = True) -> "list[dict[str, Any]]":
        """The stream as rrweb events (see :func:`webclient.replay.rrweb.to_rrweb`): the DOM
        (recorded, or synthesised from snapshots) plus every other event as a custom event --
        feed them to ``rrweb-player`` as-is."""
        from .replay.rrweb import to_rrweb

        return to_rrweb(self.events, document_id=document_id, custom=custom)

    def har(self) -> dict[str, Any]:
        """A HAR ``log`` built from the stream's network events that carry a body."""
        from .replay.har import har_from_events

        return har_from_events(self.of("network"))


def read(path: "str | Path") -> TraceReader:
    """Open a trace file for reading."""
    return TraceReader(path)
