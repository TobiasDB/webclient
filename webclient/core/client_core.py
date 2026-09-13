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
from ..pool import PoolStats
from . import live as _live
from .document_core import DocumentCore
from .reference_core import ReferenceCore, from_url
from .web_core import Backing, WebCore


class NameScope:
    """An ordered, optionally LRU-capped map of scoped names to objects. Names
    are ``{kind}:{scope:03d}-{seq:03d}``; refs and docs share the scope's seq."""

    def __init__(self, index: int, cap: int | None = None) -> None:
        from collections import OrderedDict
        self.index = index
        self.cap = cap
        self.seq = 0
        self._items: "OrderedDict[str, Any]" = OrderedDict()

    def add(self, kind: str, obj: Any) -> str:
        self.seq += 1
        name = f"{kind}:{self.index:03d}-{self.seq:03d}"
        self._items[name] = obj
        if self.cap is not None:
            while len(self._items) > self.cap:
                self._items.popitem(last=False)      # evict least-recent
        return name

    def get(self, name: str) -> Any:
        obj = self._items.get(name)
        if obj is not None:
            self._items.move_to_end(name)            # LRU touch
        return obj

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)


class _PoolView:
    """A minimal read-only view of the client's transport leases (MVP)."""

    def __init__(self, core: "WebClientCore") -> None:
        self._core = core

    def stats(self) -> PoolStats:
        c = self._core
        return PoolStats(
            http_total=1 if c._http is not None else 0,
            http_free=1 if c._http is not None else 0,
            pages_total=c._pages_created,
            pages_free=c._pages_created - len(c._pages))


class WebClientCore(WebCore, BaseModel):
    """Core Fields (policy) + engine loop + http client. The user-facing
    ``WebClient`` is the surface; this is the machinery it drives."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    timeout: float = 30.0
    default_headers: dict[str, str] = {}
    names_cap: int | None = None

    _loop: Any = PrivateAttr(default=None)
    _http: Any = PrivateAttr(default=None)     # httpx.AsyncClient (MVP: one shared)
    _render_table: dict[tuple[str, str], Any] = PrivateAttr(default_factory=dict)
    _closed: bool = PrivateAttr(default=False)
    _scope: Any = PrivateAttr(default=None)    # the client's NameScope (000)
    _scope_counter: int = PrivateAttr(default=0)   # next session scope index
    _bus: Any = PrivateAttr(default=None)      # EventBus (lazy)
    _pw: Any = PrivateAttr(default=None)       # playwright instance (lazy)
    _browser: Any = PrivateAttr(default=None)  # chromium browser (lazy)
    _pages: list = PrivateAttr(default_factory=list)   # live pages in use
    _pages_created: int = PrivateAttr(default=0)
    _sessions: list = PrivateAttr(default_factory=list)   # sessions to close

    @property
    def bus(self) -> EventBus:
        if self._bus is None:
            self._bus = EventBus()
        return self._bus

    def model_post_init(self, _ctx: Any) -> None:
        self._scope = NameScope(0, cap=self.names_cap)

    def new_scope(self) -> NameScope:
        """A fresh scope for a session (index 1, 2, ...)."""
        self._scope_counter += 1
        return NameScope(self._scope_counter)

    def _scopes(self) -> list:
        """The client scope plus every live session scope."""
        return [self._scope, *(s._scope for s in self._sessions
                               if s._scope is not None)]

    def use(self, renderer: Any) -> "WebClientCore":
        """Register a Renderer override for its (kind, format) pairs; a second
        renderer claiming the same (kind, format) shadows the first (warned)."""
        import logging
        log = logging.getLogger("webclient")
        for fmt in renderer.formats:
            existing = self._render_table.get((renderer.kind, fmt))
            if existing is not None:
                log.warning("renderer %r shadows %r for (%s, %s)",
                            renderer.name, existing.name, renderer.kind, fmt)
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
        if self._loop is not None and not self._loop.closed:
            if self._http is not None:
                self._loop.run(self._http.aclose())
            if self._browser is not None:
                self._loop.run(self._browser.close())
                self._loop.run(self._pw.stop())
        for session in self._sessions:               # cascade to sessions
            session.status = "closed"
        self._closed = True
        if self._loop is not None:
            self._loop.stop()

    # -- fetch (resolve a ReferenceCore -> DocumentCore) ---------------------
    async def afetch(self, ref: ReferenceCore, *, optional: bool = False,
                     browser: bool = False) -> DocumentCore:
        import time
        if browser:
            return await self._alive(ref)
        client = await self._client()
        headers = {**self.default_headers, **ref.headers}
        start = time.monotonic()
        try:
            resp = await engine_http.request(
                client, ref, headers=headers, cookies=ref.cookies,
                timeout=self.timeout, retries=0)
        except Exception as exc:                     # transport failure
            doc = DocumentCore(url=ref.dispatch("url"), status_code=0,
                               elapsed=time.monotonic() - start,
                               error=error_for(0, str(exc)))
            doc._client = self
            self._register(doc, ref)
            if not optional:
                raise WebException(doc.error, document=doc) from exc
            return doc
        kind = engine_http.sniff_kind(resp.headers.get("content-type"), resp.content)
        doc = DocumentCore(
            url=ref.dispatch("url"), final_url=str(resp.url), kind=kind,
            content=resp.content, status_code=resp.status_code,
            response_headers=dict(resp.headers),
            elapsed=time.monotonic() - start,
            encoding=engine_http.charset_of(resp.headers.get("content-type")))
        doc._client = self
        self._register(doc, ref)
        self._capture(doc, ref, resp)
        if not (200 <= resp.status_code < 300):
            doc.error = error_for(resp.status_code)
            if not optional:                         # loud by default
                raise WebException(doc.error, document=doc)
        return doc

    def fetch(self, ref: ReferenceCore, *, optional: bool = False,
              browser: bool = False) -> DocumentCore:
        """Sync: run ``afetch`` on the engine loop."""
        return self.loop().run(self.afetch(ref, optional=optional, browser=browser))

    # -- live / browser ------------------------------------------------------
    async def _browser_page(self) -> Any:
        if self._browser is None:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch()
        page = await self._browser.new_page()
        self._pages.append(page)
        self._pages_created += 1
        await page.add_init_script(_live.INIT_JS)
        return page

    async def _alive(self, ref: ReferenceCore,
                     replay: list[dict[str, Any]] | None = None) -> DocumentCore:
        page = await self._browser_page()
        raw: list[tuple[str, str]] = []
        page.on("console", lambda m: raw.append((m.type, m.text)))
        url = ref.dispatch("url")
        await page.goto(url)
        doc = DocumentCore(url=url, final_url=page.url, kind="html",
                           content=(await page.content()).encode(), status_code=200)
        doc._client = self
        doc._page = page
        self._register(doc, ref)
        for level, text in raw:
            doc._events.append(_live.console_event(level, text, doc))
        for step in (replay or []):                  # reproduce mutated state
            args = step.get("args", {})
            loc = page.locator(args.get("selector") or "*").first
            if step["op"] == "click":
                await loc.click()
            elif step["op"] == "write":
                await loc.fill(args.get("text", "") or "")
        # discard load/replay mutations: only post-collect interactions are
        # captured as events (so a node's event view reflects real changes).
        await page.evaluate(_live._DRAIN_JS)
        return doc

    async def _areload(self, core: DocumentCore) -> DocumentCore:
        if core._page is not None or (core._ref is not None and core._ref.actions):
            return await self._alive(core._ref, replay=list(core._ref.actions))
        return await self.afetch(core._ref)          # plain HTTP refetch

    def release(self, doc: DocumentCore) -> None:
        """Return a live document's page to the pool (close it)."""
        page = doc._page
        if page is not None:
            self.loop().run(page.close())
            if page in self._pages:
                self._pages.remove(page)
            doc._page = None

    @property
    def pool(self) -> Any:
        return _PoolView(self)

    # -- naming / recovery ---------------------------------------------------
    def _register(self, doc: DocumentCore, ref: ReferenceCore) -> None:
        """Give the reference and document scoped names in the owning scope
        (a session's, else the client's) and index them for recovery."""
        import time
        session = ref._session
        scope = session._scope if session is not None else self._scope
        if not ref.name:
            ref.name = scope.add("ref", ref)
        doc.root = ref.name
        doc.id = doc.name = scope.add("doc", doc)
        doc.created = doc.accessed = time.time()
        doc._ref = ref

    def document(self, name: str) -> DocumentCore | None:
        """Recover a materialised document by name from any live scope."""
        import time
        for scope in self._scopes():
            obj = scope.get(name)
            if isinstance(obj, DocumentCore):
                obj.accessed = time.time()
                return obj
        return None

    def reference(self, name: str) -> ReferenceCore | None:
        """Recover a reference by its (root) name from any live scope."""
        for scope in self._scopes():
            obj = scope.get(name)
            if isinstance(obj, ReferenceCore):
                return obj
        return None

    def _capture(self, doc: DocumentCore, ref: ReferenceCore, resp: Any) -> None:
        """Emit a NavigationEvent per redirect hop plus a final NetworkEvent,
        routed onto the document and published on the bus."""
        events: list[Any] = []
        for hop in resp.history:                     # redirect hops are plain
            events.append(NetworkEvent(
                request=from_url(str(hop.url)), status_code=hop.status_code,
                document_id=doc.id, source="core-network"))
        events.append(NavigationEvent(                # the landing is a navigation
            request=ref, status_code=resp.status_code, document_id=doc.id,
            source="core-network"))
        for event in events:
            self.bus.publish(event)
        doc._events.extend(events)


__all__ = ["WebClientCore", "NameScope"]
