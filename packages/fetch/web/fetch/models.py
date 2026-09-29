"""The fetch layer's data models + interfaces -- the plain value types, gathered in one place.

Pure data (no behaviour, no ``httpx`` / ``playwright`` type crosses them): the :class:`Request`
spec, the :class:`Snapshot` result, the capture :class:`~pydantic.BaseModel` event types a browser
fetch records, the :class:`Wait` readiness config, a page :class:`Script`, and the two structural
:class:`~typing.Protocol` interfaces (:class:`Fetcher` / :class:`Session`). The behaviour that uses
them lives in the sibling modules (``http`` / ``browser`` backends, ``wait.apply_wait``,
``script.ScriptRegistry``, ...); this module is a leaf -- it imports only pydantic and ``.errors``.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, JsonValue

from .errors import WebError


class Request(BaseModel):
    """One HTTP request to perform. ``url`` is complete (scheme + host + path + query). The fetch
    layer does not build URLs (join/params) -- it takes a URL and performs it."""

    url: str
    method: str = "GET"
    headers: dict[str, str] = {}
    cookies: dict[str, str] = {}
    body: bytes | None = None
    timeout: float = 30.0
    follow_redirects: bool = True


# -- capture events: what a browser fetch records onto a Snapshot (plain models with a ``topic``,
# routed structurally by the bus -- they do NOT inherit a base). --------------------------------


class FetchEvent(BaseModel):
    """One fetch a backend performed: the URL, resulting status, and how long it took. ``source``
    names the backend (e.g. ``"http"`` / ``"browser"``)."""

    topic: str = "fetch"
    url: str = ""
    status: int = 0
    elapsed: float = 0.0
    source: str = ""


class NetworkEvent(BaseModel):
    """One response seen during a fetch (method, URL, status, resource type). Carries the response
    ``body`` when recorded, which is what makes the network stream replayable -- no separate HAR.
    """

    topic: str = "network"
    method: str = ""
    url: str = ""
    status: int = 0
    resource_type: str = ""
    body: bytes = b""
    source: str = ""


class DOMEvent(BaseModel):
    """A chunk of DOM records drained from a page script (the internal form of rrweb / recorder
    output). ``records`` is the recorder's own JSON; ``script`` names the recorder."""

    topic: str = "dom"
    script: str = ""
    records: list[dict[str, JsonValue]] = []


class ConsoleEvent(BaseModel):
    """One ``console.*`` message the page logged during a browser fetch (level + text)."""

    topic: str = "console"
    level: str = ""
    text: str = ""


#: the events a :class:`Snapshot` carries -- a concrete union (not the Event Protocol) so it is a
#: valid pydantic field type.
CaptureEvent = NetworkEvent | DOMEvent | ConsoleEvent


class Snapshot(BaseModel):
    """The raw result of performing a :class:`Request`: request + response bytes + transport
    metadata + captured events. Pure data -- no ``httpx``/``playwright`` type crosses it, and it
    does NOT sniff the kind or decode a charset (that is web.parse). ``error`` is set only on a
    TRANSPORT failure (no response) -- a 404 is a valid Snapshot with ``error is None``.
    """

    request: Request
    url: str = ""  # the final URL after redirects (== request.url when there were none)
    status: int = 0  # 0 = no response (transport failure)
    headers: dict[str, str] = {}
    content: bytes = b""
    elapsed: float = 0.0
    set_cookies: dict[str, str] = {}
    redirects: list[str] = []  # the intermediate URLs, in order
    events: list[CaptureEvent] = []  # captured during the fetch (browser: network/DOM/console)
    error: WebError | None = None

    @property
    def ok(self) -> bool:
        """A response arrived with a 2xx status (a 404 is a valid Snapshot but not ``ok``)."""
        return self.error is None and 200 <= self.status < 300


#: the browser readiness milestones (see :class:`Wait`).
Until = Literal["domcontentloaded", "load", "networkidle", "dom_stable", "selector"]


class Wait(BaseModel):
    """When the browser backend snapshots. The DEFAULT is ``dom_stable`` -- wait until the page
    stops rewriting its own DOM (an SPA settling), returning at the ``timeout`` budget even if it
    never fully settles (a bounded settle, not a failure). ``timeout`` bounds the whole wait;
    ``quiet`` is the no-change window that counts as stable; ``selector`` targets ``until='selector'``.
    """

    until: Until = "dom_stable"
    timeout: float = 8.0
    quiet: float = 0.4
    selector: "str | None" = None


class Script(BaseModel):
    """A page script the browser transport injects. ``on`` picks the lifecycle stage: ``init``
    (before navigation), ``load`` (after the page settles), or ``snapshot`` (a DOM transform run at
    EACH snapshot, before the HTML is read -- e.g. inlining shadow roots / frames). ``drain``
    (optional) is a JS expression the fetcher evaluates after the page settles to pull buffered
    output into an event."""

    name: str
    js: str
    on: Literal["init", "load", "snapshot"] = "load"
    drain: str = ""


@runtime_checkable
class Fetcher(Protocol):
    """Performs one request and returns its Snapshot. Never raises for a transport failure (that
    becomes ``snapshot.error``); does transport only. HTTP / Browser / Replay all implement it, so
    callers depend on the interface, not on httpx/playwright. Close with :meth:`aclose`.
    """

    async def fetch(self, request: Request) -> Snapshot: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class Session(Protocol):
    """A stateful, resource-owning fetch handle. ``fetch`` performs a request within the session
    (state persists); ``aclose`` releases everything it owns. Page ownership lives on the session
    (the key lesson from the old client): a live browser page belongs to a session and dies with
    it. A backend's one-shot ``fetch`` is a session opened and closed for one request.
    """

    async def fetch(self, request: Request) -> Snapshot: ...

    async def aclose(self) -> None: ...


__all__ = [
    "Request",
    "Snapshot",
    "Wait",
    "Until",
    "Script",
    "Fetcher",
    "Session",
    "FetchEvent",
    "NetworkEvent",
    "DOMEvent",
    "ConsoleEvent",
    "CaptureEvent",
]
