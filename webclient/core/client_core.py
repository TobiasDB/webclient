"""WebClientCore: the engine core (MVP).

Owns the engine loop + an http client; its backings are the client's verbs
(fetch for now; search/crawl/session later). Everything async; the sync
surface bridges onto the loop. Remote will swap ``aexecute`` for a round-trip.
"""
from __future__ import annotations

from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, ConfigDict, PrivateAttr

from ..engine import http as engine_http
from ..engine.loop import EngineLoop
from ..errors import WebException, error_for
from ..events import EventBus, NavigationEvent, NetworkEvent
from .document_core import DocumentCore
from .reference_core import ReferenceCore, from_url
from .web_core import Backing, WebCore


class WebClientCore(WebCore, BaseModel):
    """Core Fields (policy) + engine loop + http client. The user-facing
    ``WebClient`` is the surface; this is the machinery it drives."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    timeout: float = 30.0
    default_headers: dict[str, str] = {}

    _loop: Any = PrivateAttr(default=None)
    _http: Any = PrivateAttr(default=None)     # httpx.AsyncClient (MVP: one shared)
    _render_table: dict[tuple[str, str], Any] = PrivateAttr(default_factory=dict)
    _closed: bool = PrivateAttr(default=False)
    _docs: dict[str, Any] = PrivateAttr(default_factory=dict)   # name -> DocumentCore
    _refs: dict[str, Any] = PrivateAttr(default_factory=dict)   # root -> ReferenceCore
    _counter: int = PrivateAttr(default=0)
    _bus: Any = PrivateAttr(default=None)      # EventBus (lazy)

    @property
    def bus(self) -> EventBus:
        if self._bus is None:
            self._bus = EventBus()
        return self._bus

    def use(self, renderer: Any) -> "WebClientCore":
        """Register a Renderer override for its (kind, format) pairs."""
        for fmt in renderer.formats:
            self._render_table[(renderer.kind, fmt)] = renderer
        return self

    BACKINGS: ClassVar[tuple[Backing, ...]] = ()   # TODO: (Fetch, Search, ...) as ops

    # -- loop / lifecycle ----------------------------------------------------
    def loop(self) -> EngineLoop:
        if self._loop is None:
            self._loop = EngineLoop()
        return self._loop

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(follow_redirects=True)
        return self._http

    def close(self) -> None:
        if self._closed:
            return
        if self._loop is not None and not self._loop.closed and self._http is not None:
            self._loop.run(self._http.aclose())
        self._closed = True
        if self._loop is not None:
            self._loop.stop()

    # -- fetch (resolve a ReferenceCore -> DocumentCore) ---------------------
    async def afetch(self, ref: ReferenceCore, *, optional: bool = False) -> DocumentCore:
        client = await self._client()
        headers = {**self.default_headers, **ref.headers}
        resp = await engine_http.request(
            client, ref, headers=headers, cookies=ref.cookies,
            timeout=self.timeout, retries=0)
        kind = engine_http.sniff_kind(resp.headers.get("content-type"), resp.content)
        doc = DocumentCore(
            url=ref.dispatch("url"), final_url=str(resp.url), kind=kind,
            content=resp.content, status_code=resp.status_code,
            response_headers=dict(resp.headers),
            encoding=engine_http.charset_of(resp.headers.get("content-type")))
        doc._client = self
        self._register(doc, ref)
        self._capture(doc, ref, resp)
        if not (200 <= resp.status_code < 300):
            doc.error = error_for(resp.status_code)
            if not optional:                         # loud by default
                raise WebException(doc.error)
        return doc

    def fetch(self, ref: ReferenceCore, *, optional: bool = False) -> DocumentCore:
        """Sync: run ``afetch`` on the engine loop."""
        return self.loop().run(self.afetch(ref, optional=optional))

    # -- naming / recovery ---------------------------------------------------
    def _register(self, doc: DocumentCore, ref: ReferenceCore) -> None:
        """Give the document and its reference scoped names and index them so
        they can be recovered (and so ``doc.ref()`` round-trips to identity)."""
        self._counter += 1
        n = self._counter
        ref.name = ref.name or f"ref{n}"
        doc.name, doc.root = f"doc{n}", ref.name
        doc._ref = ref
        self._docs[doc.name] = doc
        self._refs[ref.name] = ref

    def _capture(self, doc: DocumentCore, ref: ReferenceCore, resp: Any) -> None:
        """Emit a NavigationEvent per redirect hop plus a final NetworkEvent,
        routed onto the document and published on the bus."""
        events: list[Any] = []
        for hop in resp.history:                     # each redirect
            events.append(NavigationEvent(
                request=from_url(str(hop.url)), status_code=hop.status_code,
                document_id=doc.name))
        events.append(NetworkEvent(
            request=ref, status_code=resp.status_code, document_id=doc.name))
        for event in events:
            self.bus.publish(event)
        doc._events.extend(events)

    def document(self, name: str) -> DocumentCore:
        """Recover a materialised document by name."""
        return self._docs[name]

    def reference(self, name: str) -> ReferenceCore:
        """Recover a reference by its (root) name."""
        return self._refs[name]


__all__ = ["WebClientCore"]
