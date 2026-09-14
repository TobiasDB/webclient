"""WebClientCore: the engine core.

Holds the engine loop, a ``ClientPool`` (leasing http clients + browser pages),
the event bus, renderer plugins and name scopes, and the machinery that drives
transport (``afetch``) and plan execution (``execute``/``aexecute``). Its
user-facing features are backings (``FetchBacking``: ref/fetch/summary); the
``WebClient`` / ``AsyncWebClient`` surface is a thin sync/async/lazy interface
over it. A remote backend is just a subclass that swaps ``execute`` for an HTTP
round-trip -- so the surface is unchanged; only the core differs.
"""

from __future__ import annotations

import threading
from typing import Any, ClassVar, cast

from pydantic import BaseModel, ConfigDict, PrivateAttr

from ..engine import http as engine_http
from ..engine.loop import EngineLoop
from ..errors import WebError, WebException, error_for
from ..events import EventBus, NavigationEvent, NetworkEvent
from . import live as _live
from .document_core import DocumentCore
from .reference_core import HttpMethod, ReferenceCore, from_url
from .web_core import Backing, WebCore


def _materialize(result: Any) -> Any:
    """A materialised plan result: a scalar leaf becomes a ``Field``; a surface
    or collection passes through."""
    from ..collection import Field

    if isinstance(result, Field):
        return result
    if isinstance(result, (str, int, float, bool)) or result is None:
        return Field(result)
    return result


def _retry_after_seconds(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header (delta-seconds or an HTTP-date) into a
    non-negative delay, or ``None`` if absent/unparseable."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(int(value)))
    except ValueError:
        pass
    try:
        import time
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(value)
        return max(0.0, when.timestamp() - time.time())
    except Exception:
        return None


class NameScope:
    """An ordered, optionally LRU-capped map of scoped names to objects. Names
    are ``{kind}:{scope:03d}-{seq:03d}``; refs and docs share the scope's seq.

    A lock guards every mutation: ``add`` runs on the engine loop (registering a
    resolved doc/ref) while ``get`` runs on the caller's thread (name recovery),
    so the ``OrderedDict``'s ``move_to_end``/``popitem`` and the ``seq`` counter
    can be touched from two threads at once -- unsynchronised that corrupts the
    dict (mutated-during-iteration / lost names)."""

    def __init__(self, index: int, cap: int | None = None) -> None:
        from collections import OrderedDict

        self.index = index
        self.cap = cap
        self.seq = 0
        self._items: "OrderedDict[str, Any]" = OrderedDict()
        self._lock = threading.Lock()

    def add(self, kind: str, obj: Any) -> str:
        with self._lock:
            self.seq += 1
            name = f"{kind}:{self.index:03d}-{self.seq:03d}"
            self._items[name] = obj
            if self.cap is not None:
                while len(self._items) > self.cap:
                    self._items.popitem(last=False)  # evict least-recent
            return name

    def get(self, name: str) -> Any:
        with self._lock:
            obj = self._items.get(name)
            if obj is not None:
                self._items.move_to_end(name)  # LRU touch
            return obj

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


class FetchBacking(Backing):
    """The client's authoring verbs -- eager and real like every backing op,
    returning real cores/values (``ref -> ReferenceCore``, ``fetch ->
    DocumentCore``, ``summary -> dict``). It is the *surface* that is lazy: the
    client records these calls into a plan and the executor dispatches them here
    at run time (the IO ops hand back a coroutine when already on the engine
    loop, bridged otherwise -- like ``ReferenceCore.resolve``)."""

    provides = frozenset({"ref", "lazy", "fetch", "summary"})
    gate = "ok"

    def ref(
        self, core: "WebClientCore", url: Any, method: str = "get", **kw: Any
    ) -> "ReferenceCore":
        """A client-bound reference. ``url`` may be a URL string, a ``Reference``
        surface, or a ``ReferenceCore``."""
        spec = getattr(url, "_core", url)  # unwrap a Reference surface
        if not isinstance(spec, ReferenceCore):
            spec = from_url(url, cast(HttpMethod, method), **kw)
        spec._client = core
        return spec

    #: the same client-bound reference under its authoring alias.
    lazy = ref

    def fetch(
        self,
        core: "WebClientCore",
        url: Any,
        *,
        optional: bool = False,
        error: Any = None,
        **kw: Any,
    ) -> "DocumentCore":
        """Resolve ``ref(url)`` into a document (via the reference's resolve op,
        so it is async-aware on the engine loop)."""
        ref = self.ref(core, url, **kw)
        return cast(
            DocumentCore, ref.dispatch("resolve", optional=optional, error=error)
        )

    def summary(self, core: "WebClientCore", url: Any, **kw: Any) -> "dict[str, Any]":
        """Resolve ``url`` to a title + markdown digest (async-aware)."""
        ref = self.ref(core, url, **kw)

        async def run() -> "dict[str, Any]":
            doc = await core.afetch(ref)
            return cast("dict[str, Any]", doc.dispatch("summary"))

        loop = core.loop()
        return cast(
            "dict[str, Any]", run() if loop.on_loop_thread() else loop.run(run())
        )


class WebClientCore(WebCore, BaseModel):
    """The engine: Core Fields (policy) + machinery (loop, ClientPool, bus, name
    scopes, transport + plan execution). Its user-facing features are backings
    (``FetchBacking``); the surface (``WebClient`` / ``AsyncWebClient``) is a
    thin sync/async/lazy interface over it, and a remote backend is just a
    subclass that swaps ``execute``. Sessions are a scoped subclass."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    timeout: float = 30.0
    default_headers: dict[str, str] = {}
    names_cap: int | None = None
    block_private_hosts: bool = False  # opt-in SSRF guard (loopback/private/etc.)
    retries: int = 0  # extra attempts on a retriable failure (transport/429/5xx)
    retry_backoff: float = 0.2  # base seconds; doubled each attempt (exp backoff)
    min_interval: float = 0.0  # per-host politeness: min seconds between requests

    _loop: Any = PrivateAttr(default=None)
    _pool: Any = PrivateAttr(default=None)  # ClientPool (lazy)
    _render_table: dict[tuple[str, str], Any] = PrivateAttr(default_factory=dict)
    _closed: bool = PrivateAttr(default=False)
    _scope: Any = PrivateAttr(default=None)  # the client's NameScope (000)
    _scope_counter: int = PrivateAttr(default=0)  # next session scope index
    _scope_lock: Any = PrivateAttr(default_factory=threading.Lock)  # guards ^
    _bus: Any = PrivateAttr(default=None)  # EventBus (lazy)
    _sessions: list[Any] = PrivateAttr(default_factory=list)  # sessions to close
    _host_next: dict[str, float] = PrivateAttr(  # host -> earliest next request time
        default_factory=dict
    )

    @property
    def bus(self) -> EventBus:
        if self._bus is None:
            self._bus = EventBus()
        return cast(EventBus, self._bus)

    def model_post_init(self, _ctx: Any) -> None:
        self._scope = NameScope(0, cap=self.names_cap)
        self._init_transport()

    def _init_transport(self) -> None:
        """Build the transport pool eagerly (cheap -- no browser launch until a
        page is leased) so it is never lazily created from two threads at once.
        Sessions override this to share the parent's pool."""
        from ..engine.clients import BrowserFactory, HTTPXFactory
        from ..pool import ClientPool

        self._pool = ClientPool(
            {"http": HTTPXFactory(), "page": BrowserFactory(init_script=_live.INIT_JS)},
            limits={"http": 10, "page": 4},
        )

    def new_scope(self) -> NameScope:
        """A fresh scope for a session (index 1, 2, ...). Locked so two sessions
        created off-thread cannot collide on the same scope index."""
        with self._scope_lock:
            self._scope_counter += 1
            index = self._scope_counter
        return NameScope(index)

    def _scopes(self) -> list[Any]:
        """The client scope plus every live session scope."""
        return [
            self._scope,
            *(s._scope for s in self._sessions if s._scope is not None),
        ]

    def use(self, renderer: Any) -> "WebClientCore":
        """Register a Renderer override for its (kind, format) pairs; a second
        renderer claiming the same (kind, format) shadows the first (warned)."""
        import logging

        log = logging.getLogger("webclient")
        for fmt in renderer.formats:
            existing = self._render_table.get((renderer.kind, fmt))
            if existing is not None:
                log.warning(
                    "renderer %r shadows %r for (%s, %s)",
                    renderer.name,
                    existing.name,
                    renderer.kind,
                    fmt,
                )
            self._render_table[(renderer.kind, fmt)] = renderer
        return self

    BACKINGS: ClassVar[tuple[Backing, ...]] = (FetchBacking(),)

    # -- loop / lifecycle ----------------------------------------------------
    def loop(self) -> EngineLoop:
        if self._loop is None:
            self._loop = EngineLoop()
        return cast(EngineLoop, self._loop)

    @property
    def pool(self) -> Any:
        """The transport-lease pool (http clients + browser pages)."""
        return self._pool

    def close(self) -> None:
        if self._closed:
            return
        if self._loop is not None and not self._loop.closed and self._pool is not None:
            self._loop.run(self._pool.aclose())
        for session in self._sessions:  # cascade to sessions
            session.status = "closed"
        self._closed = True
        if self._loop is not None:
            self._loop.stop()

    async def _host_blocked(self, ref: ReferenceCore) -> bool:
        """Whether ``ref``'s host resolves to a loopback / private / link-local /
        reserved address (the SSRF guard, when ``block_private_hosts``). Resolves
        names too, so a public name pointing at an internal IP is caught; an
        unresolvable host is left for the transport to fail normally."""
        import asyncio
        import ipaddress
        import socket

        def _ip_blocked(text: str) -> bool:
            ip = ipaddress.ip_address(text)
            return (
                ip.is_loopback
                or ip.is_private
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
                or ip.is_unspecified
            )

        host = ref.hostname
        if not host:
            return False
        try:
            return _ip_blocked(host)  # an IP literal
        except ValueError:
            pass
        if host == "localhost" or host.endswith(".localhost"):
            return True
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, None)
        except socket.gaierror:
            return False  # unresolvable -> let the transport surface the failure
        return any(_ip_blocked(str(info[4][0])) for info in infos)

    # -- transport (machinery): resolve a ReferenceCore -> DocumentCore ------
    async def _pace(self, host: str) -> None:
        """Politeness: keep at least ``min_interval`` seconds between requests to
        ``host`` (best-effort; concurrent same-host fetches may still bunch -- a
        per-host token bucket would be the strict form)."""
        import asyncio
        import time

        wait = self._host_next.get(host, 0.0) - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._host_next[host] = time.monotonic() + self.min_interval

    async def _afetch_once(
        self, ref: ReferenceCore, headers: dict[str, str]
    ) -> "tuple[DocumentCore, Any]":
        """One transport attempt -> ``(doc, resp)``; ``doc.error`` is set on a
        transport failure or a non-2xx status. Never raises, never registers --
        the caller (``afetch``) retries, then registers/raises the final doc."""
        import time

        start = time.monotonic()
        try:
            async with await self.pool.lease("http") as lease:
                resp = await lease.client.send(
                    ref, headers=headers, cookies=ref.cookies, timeout=self.timeout
                )
        except Exception as exc:  # transport failure
            doc = DocumentCore(
                url=ref.dispatch("url"),
                status_code=0,
                elapsed=time.monotonic() - start,
                error=error_for(0, str(exc)),
            )
            doc._client = self
            return doc, None
        kind = engine_http.sniff_kind(resp.headers.get("content-type"), resp.content)
        doc = DocumentCore(
            url=ref.dispatch("url"),
            final_url=str(resp.url),
            kind=kind,
            content=resp.content,
            status_code=resp.status_code,
            response_headers=dict(resp.headers),
            elapsed=time.monotonic() - start,
            encoding=engine_http.charset_of(resp.headers.get("content-type")),
        )
        doc._client = self
        doc._set_cookies = dict(resp.cookies)  # httpx parses Set-Cookie correctly
        if not (200 <= resp.status_code < 300):
            doc.error = error_for(resp.status_code)
        return doc, resp

    async def afetch(
        self, ref: ReferenceCore, *, optional: bool = False, browser: bool = False
    ) -> DocumentCore:
        """Resolve ``ref`` into a document over a leased transport (http) or a
        browser page. The core's own IO -- the ``fetch`` backing verb records a
        plan; this is what the executor runs when that plan resolves. A retriable
        failure (transport / 429 / 5xx) is retried up to ``retries`` times with
        exponential backoff."""
        import asyncio

        if self.block_private_hosts and await self._host_blocked(ref):
            doc = DocumentCore(
                url=ref.dispatch("url"),
                status_code=0,
                error=WebError(
                    type="BlockedHost",
                    message=f"host {ref.hostname!r} is blocked by policy",
                ),
            )
            doc._client = self
            self._register(doc, ref)
            if not optional:
                raise WebException(cast(WebError, doc.error), document=doc)
            return doc
        if browser:
            return await self._alive(ref)
        headers = {**self.default_headers, **ref.headers}
        if self.min_interval > 0.0:
            await self._pace(ref.hostname)
        doc, resp = await self._afetch_once(ref, headers)
        attempt = 0
        while doc.error is not None and doc.error.retriable and attempt < self.retries:
            delay = self.retry_backoff * (2**attempt)
            if resp is not None:  # honour a server-sent Retry-After (429/503)
                after = _retry_after_seconds(resp.headers.get("retry-after"))
                if after is not None:
                    delay = min(after, 60.0)  # cap so a huge value can't stall us
            await asyncio.sleep(delay)
            attempt += 1
            doc, resp = await self._afetch_once(ref, headers)
        self._register(doc, ref)
        if resp is not None:  # emit navigation/network events for the final doc
            self._capture(doc, ref, resp)
        if doc.error is not None and not optional:  # loud by default
            raise WebException(doc.error, document=doc)
        return doc

    # -- plan execution (machinery): the surface's sync/async entry ----------
    def execute(self, expr: Any, context: Any = None, *, stream: bool = False) -> Any:
        """Run a recorded plan on this engine (sync bridge). A remote subclass
        swaps this for an HTTP round-trip; ``stream=True`` yields rows."""
        from ..query.executor import evaluate

        if stream:
            return self._stream(expr, context)
        return _materialize(evaluate(expr, context, client=self))

    async def aexecute(self, expr: Any, context: Any = None) -> Any:
        """Await a plan on the engine loop without blocking the caller's loop."""
        import asyncio

        from ..query.executor import aevaluate

        result = await asyncio.wrap_future(
            self.loop().submit(aevaluate(expr, context, client=self))
        )
        return _materialize(result)

    def _stream(self, expr: Any, context: Any) -> Any:
        """Bridge the async row stream to a sync iterator, publishing plan
        events (a ``_pump`` task feeds a bounded queue on the engine loop)."""
        from ..collection import Field
        from ..events import PlanEvent
        from ..query.executor import astream

        self.bus.publish(PlanEvent(phase="started"))
        count = 0
        for row in self.loop().stream(astream(expr, context, client=self)):
            count += 1
            self.bus.publish(PlanEvent(phase="row"))
            yield row.get() if isinstance(row, Field) else row
        self.bus.publish(PlanEvent(phase="done", detail={"rows": count}))

    async def astream(self, expr: Any, context: Any) -> Any:
        """Async row stream (the same truly-incremental rows as ``_stream``,
        bridged from the engine loop to the caller's loop as they complete)."""
        from ..collection import Field
        from ..events import PlanEvent
        from ..query.executor import astream as _astream

        self.bus.publish(PlanEvent(phase="started"))
        count = 0
        async for row in self.loop().astream(_astream(expr, context, client=self)):
            count += 1
            self.bus.publish(PlanEvent(phase="row"))
            yield row.get() if isinstance(row, Field) else row
        self.bus.publish(PlanEvent(phase="done", detail={"rows": count}))

    # -- sessions ------------------------------------------------------------
    def session(
        self,
        *,
        ttl: float | None = None,
        headers: dict[str, str] | None = None,
        **kw: Any,
    ) -> Any:
        """A new session sharing this engine (a scoped ``WebSessionCore``)."""
        from .session_core import WebSessionCore

        core = WebSessionCore(ttl=ttl, session_headers=headers or {}, **kw)
        core.bind(self)
        return core

    # -- live / browser ------------------------------------------------------
    async def _alive(
        self, ref: ReferenceCore, replay: list[dict[str, Any]] | None = None
    ) -> DocumentCore:
        lease = await self.pool.lease("page")
        try:
            page = lease.client.page
            raw: list[tuple[str, str]] = []
            page.on("console", lambda m: raw.append((m.type, m.text)))
            url = ref.dispatch("url")
            await page.goto(url)
            doc = DocumentCore(
                url=url,
                final_url=page.url,
                kind="html",
                content=(await page.content()).encode(),
                status_code=200,
            )
            doc._client = self
            doc._page = page
            doc._lease = lease
            self._register(doc, ref)
            for level, text in raw:
                doc._events.append(_live.console_event(level, text, doc))
            for step in replay or []:  # reproduce mutated state
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
        except BaseException:
            await self.pool.release(lease)  # never leak the page lease on failure
            raise

    async def _areload(self, core: DocumentCore) -> DocumentCore:
        if core._page is not None or (core._ref is not None and core._ref.actions):
            return await self._alive(core._ref, replay=list(core._ref.actions))
        return await self.afetch(cast(ReferenceCore, core._ref))  # plain HTTP refetch

    def release(self, doc: DocumentCore) -> None:
        """Return a live document's page lease to the pool."""
        if doc._lease is not None:
            self.loop().run(self.pool.release(doc._lease))
            doc._lease = None
            doc._page = None

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
        for hop in resp.history:  # redirect hops are plain
            events.append(
                NetworkEvent(
                    request=from_url(str(hop.url)),
                    status_code=hop.status_code,
                    document_id=doc.id,
                    source="core-network",
                )
            )
        events.append(
            NavigationEvent(  # the landing is a navigation
                request=ref,
                status_code=resp.status_code,
                document_id=doc.id,
                source="core-network",
            )
        )
        for event in events:
            self.bus.publish(event)
        doc._events.extend(events)


__all__ = ["WebClientCore", "NameScope", "FetchBacking"]
