"""Telemetry: typed records a backing writes directly.

There is no bus, no topics, no correlation ids and no subscription to
establish before navigation — the object that produces a record owns the list
it goes in. Consumers that want a stream register a handler by record *class*.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar


@dataclass
class Record:
    """Base for anything a backing reports."""

    at: float = field(default_factory=time.time)


@dataclass
class RequestRecord(Record):
    url: str = ""
    method: str = "get"
    status: int = 0
    ms: float = 0.0
    kind: str = "document"          # document / xhr / fetch / asset / script
    from_cache: bool = False

    @property
    def host(self) -> str:
        from urllib.parse import urlparse
        return urlparse(self.url).hostname or ""


@dataclass
class RedirectRecord(Record):
    from_url: str = ""
    to_url: str = ""
    status: int = 0


@dataclass
class ConsoleRecord(Record):
    level: str = "log"
    text: str = ""


@dataclass
class RetryRecord(Record):
    url: str = ""
    attempt: int = 0
    reason: str = ""


R = TypeVar("R", bound=Record)


class Telemetry:
    """What one Document observed. Scoped to that document, for its whole
    life — a navigated-away Document keeps its own."""

    def __init__(self, observers: "Observers | None" = None) -> None:
        self.requests: list[RequestRecord] = []
        self.redirects: list[RedirectRecord] = []
        self.console: list[ConsoleRecord] = []
        self.retries: list[RetryRecord] = []
        self._observers = observers

    _BUCKETS: dict[type, str] = {}

    def add(self, record: Record) -> None:
        bucket = {RequestRecord: "requests", RedirectRecord: "redirects",
                  ConsoleRecord: "console", RetryRecord: "retries"}
        for cls, name in bucket.items():
            if isinstance(record, cls):
                getattr(self, name).append(record)
                break
        if self._observers is not None:
            self._observers.emit(record)

    @property
    def all(self) -> list[Record]:
        merged = [*self.requests, *self.redirects, *self.console, *self.retries]
        return sorted(merged, key=lambda r: r.at)

    def __repr__(self) -> str:
        return (f"Telemetry(requests={len(self.requests)}, "
                f"redirects={len(self.redirects)}, "
                f"console={len(self.console)}, retries={len(self.retries)})")


class Observers:
    """Class-dispatched handlers. `wc.on(RequestRecord, handler)`."""

    def __init__(self) -> None:
        self._handlers: list[tuple[type, Callable[[Any], None]]] = []
        self._lock = threading.Lock()

    def on(self, record_type: type[R],
           handler: Callable[[R], None]) -> Callable[[], None]:
        with self._lock:
            entry = (record_type, handler)
            self._handlers.append(entry)

        def cancel() -> None:
            with self._lock:
                if entry in self._handlers:
                    self._handlers.remove(entry)
        return cancel

    def emit(self, record: Record) -> None:
        with self._lock:
            handlers = list(self._handlers)
        for record_type, handler in handlers:
            if isinstance(record, record_type):
                handler(record)
