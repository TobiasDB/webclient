"""WebClient: lifecycle root and facade.

Owns the ClientPool, EventBus, EventRegistry, plugins and the document
registry. M2 scope: http fetch, plugin attach/detach, renderer table,
default client. Sessions land in M3, browser in M4, plans in M6.
"""
from __future__ import annotations

import atexit
import logging
import threading
import weakref
from typing import TYPE_CHECKING, Any, Literal, Sequence
from uuid import uuid4

if TYPE_CHECKING:
    from .session import Session

import httpx
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr
from typing_extensions import Self

from .engine import http as engine_http
from .engine.browser import BrowserHost
from .engine.loop import EngineLoop
from .events import EventBus, EventRegistry
from .live import LiveDocument
from .models import (
    Document,
    FetchError,
    HttpMethod,
    Proxy,
    Reference,
    Script,
)
from .plugins.base import Plugin, Renderer, Surface, SurfaceKind
from .plugins.network import HttpNetworkPlugin
from .plugins.page import PageConsolePlugin, PageDomPlugin, PageNetworkPlugin
from .plugins.render import core_renderers
from .pool import ClientPool

logger = logging.getLogger("webclient")


def _core_plugins() -> list[Plugin]:
    return [HttpNetworkPlugin(), PageNetworkPlugin(), PageConsolePlugin(),
            PageDomPlugin(), *core_renderers()]


class WebClient(BaseModel):
    """Lifecycle root. References, Documents and LiveDocuments are bound to
    the WebClient that made them; closing it reclaims everything."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # policy
    timeout: float = 30.0
    retries: int = 0                 # transport errors only (ISSUES #20)
    events_cap: int = 1000           # per-document event-store cap
    verify_tls: bool = True
    default_headers: dict[str, str] = Field(default_factory=dict)
    proxy_pool: list[Proxy] = Field(default_factory=list)
    headless: bool = True
    default_scripts: list[Script] = Field(default_factory=list)

    # owned infrastructure (default_factory: these hold runtime state)
    pool: ClientPool = Field(default_factory=ClientPool)
    bus: EventBus = Field(default_factory=EventBus)
    registry: EventRegistry = Field(default_factory=EventRegistry)
    plugins: list[Plugin] = Field(default_factory=list)

    _loop: EngineLoop | None = PrivateAttr(default=None)
    _loop_lock: Any = PrivateAttr(default_factory=threading.Lock)
    _documents: dict[str, Any] = PrivateAttr(default_factory=dict)
    _sessions: dict[str, Any] = PrivateAttr(default_factory=dict)
    _live: dict[str, Any] = PrivateAttr(default_factory=dict)  # strong refs
    _browser: Any = PrivateAttr(default=None)
    _exec: Any = PrivateAttr(default=None)
    _render_table: dict[tuple[str, str], Renderer] = PrivateAttr(default_factory=dict)
    _closed: bool = PrivateAttr(default=False)

    def model_post_init(self, __context: Any) -> None:
        self.pool._owner = self
        for plugin in _core_plugins():
            self.use(plugin)

    # -- lifecycle -----------------------------------------------------------
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        if self._loop is not None and not self._loop.closed:
            for live in list(self._live.values()):
                try:
                    self._loop.run(self._release_live(live))
                except Exception:
                    pass
            for sess in list(self._sessions.values()):
                try:
                    sess.close()   # persists browser storage_state
                except Exception:
                    pass
            if self._browser is not None:
                self._loop.run(self._browser.aclose())
            self._loop.run(self.pool._aclose())
        self._closed = True
        for sess in self._sessions.values():
            sess.status = "closed"
        if self._loop is not None:
            self._loop.stop()

    def _ensure_loop(self) -> EngineLoop:
        if self._closed:
            raise RuntimeError("WebClient is closed")
        with self._loop_lock:
            if self._loop is None:
                self._loop = EngineLoop()
        return self._loop

    # -- plugins -------------------------------------------------------------
    def use(self, plugin: Plugin) -> Self:
        """Register a plugin: same name replaces; a Renderer claiming an
        occupied (kind, format) shadows it with a warning (ISSUES #16)."""
        self.plugins[:] = [p for p in self.plugins if p.name != plugin.name]
        self.plugins.append(plugin)
        for event_cls in plugin.events:
            self.registry.register(event_cls)
        if isinstance(plugin, Renderer):
            for fmt in plugin.formats:
                key = (plugin.kind, fmt)
                holder = self._render_table.get(key)
                if holder is not None and holder.name != plugin.name:
                    logger.warning(
                        "renderer %r shadows %r for %s", plugin.name,
                        holder.name, key)
                self._render_table[key] = plugin
        return self

    def _attach(self, kind: SurfaceKind, raw: Any, *,
                session_id: str | None = None,
                document_id: str | None = None,
                plan_id: str | None = None) -> list[tuple[Plugin, Surface]]:
        attached = []
        for plugin in self.plugins:
            if kind not in plugin.surfaces:
                continue
            surface = Surface(kind=kind, raw=raw, session_id=session_id,
                              document_id=document_id, plan_id=plan_id)
            surface._bus = self.bus
            surface._source = plugin.name
            plugin.attach(surface)
            attached.append((plugin, surface))
        return attached

    @staticmethod
    def _detach(attached: list[tuple[Plugin, Surface]]) -> None:
        for plugin, surface in reversed(attached):
            plugin.detach(surface)

    @staticmethod
    async def _run_attach_async(
            attached: list[tuple[Plugin, Surface]]) -> None:
        """Await plugins' optional async setup (ISSUES #29: additive
        `attach_async` hook for setup that must be awaited, e.g. playwright
        expose_binding)."""
        for plugin, surface in attached:
            hook = getattr(plugin, "attach_async", None)
            if hook is not None:
                await hook(surface)

    # -- references / fetching ----------------------------------------------
    def ref(self, url: str, method: HttpMethod = "get",
            **kwargs: Any) -> Reference:
        return Reference.from_url(url, method=method, **kwargs).bind(self)

    def fetch(self, ref: Reference, *, browser: bool = False,
              session: Any = None,
              scripts: Sequence[Script] | None = None,
              wait_until: str = "load",
              optional: bool = False) -> Document:
        if browser:
            return self._ensure_loop().run(self._fetch_browser(
                ref, session=session, scripts=scripts,
                wait_until=wait_until, optional=optional))
        return self._ensure_loop().run(
            self._fetch(ref, optional=optional, session=session))

    # -- browser path (M4) ----------------------------------------------------
    def _browser_host(self) -> BrowserHost:
        if self._browser is None:
            self._browser = BrowserHost(headless=self.headless)
        return self._browser

    async def _fetch_browser(self, ref: Reference, *, session: Any,
                             scripts: Sequence[Script] | None,
                             wait_until: str, optional: bool) -> LiveDocument:
        session = session or ref._session
        if session is not None:
            session.check()
        lease = await self.pool._acquire("page", session=session)
        page = lease._page
        for plugin in self.plugins:
            if "page" in plugin.surfaces:
                for script in plugin.scripts:
                    await page.add_init_script(script.source)
        for script in (*self.default_scripts, *(scripts or ())):
            await page.add_init_script(script.source)
        headers = {**(session.headers if session else {}), **ref.headers}
        if headers:
            await page.set_extra_http_headers(headers)
        if session is not None and session.cookies:
            await page.context.add_cookies([
                {"name": k, "value": v, "url": ref.url}
                for k, v in session.cookies.items()])
        document_id = uuid4().hex
        # Subscribe routing BEFORE navigation: console/xhr/dom events fire
        # during goto, so the store must be listening first.
        events: list[Any] = []
        routing = self._route_events(document_id, events)
        attached = self._attach("page", page, document_id=document_id,
                                session_id=session.id if session else None)
        await self._run_attach_async(attached)
        try:
            response = await page.goto(ref.url, wait_until=wait_until)
        except Exception as exc:
            self._detach(attached)
            routing.cancel()
            await self.pool._release(lease)
            if optional:
                doc = self._build_document(ref, document_id, None, [], session)
                return doc  # type: ignore[return-value]
            raise FetchError(f"browser {ref.url}: {exc}") from exc
        live = await self._wrap_live_page(ref, document_id, page, response,
                                          lease, session, attached, events,
                                          routing)
        if not optional and not live.ok:
            await self._release_live(live)
            raise FetchError(
                f"browser {ref.url} -> {live.status_code}", document=live)
        return live

    def _route_events(self, document_id: str, events: list[Any]) -> Any:
        def route(event: Any) -> None:
            events.append(event)
            if len(events) > self.events_cap:
                del events[0]
        return self.bus.subscribe("", route, document_id=document_id)

    async def _wrap_live_page(self, ref: Reference, document_id: str,
                              page: Any, response: Any, lease: Any,
                              session: Any, attached: list[Any],
                              events: list[Any], routing: Any) -> LiveDocument:
        fields = {name: getattr(ref, name) for name in Reference.model_fields}
        live = LiveDocument(
            **fields,
            id=document_id,
            kind="html",
            content=(await page.content()).encode(),
            status_code=response.status if response is not None else 200,
            response_headers=dict(response.headers) if response is not None else {},
            final_url=page.url,
        )
        live._client = self
        live._session = session
        live._page = page
        live._lease = lease
        live._attached = attached
        live._routing = routing
        live.events.extend(events)           # events captured during goto
        # keep routing appending to the live document's own list
        routing.cancel()
        live._routing = self._route_events(document_id, live.events)
        if session is not None:
            live.session_id = session.id
            for cookie in await page.context.cookies():
                session.cookies[cookie["name"]] = cookie["value"]
        self._documents[document_id] = weakref.ref(live)
        self._live[document_id] = live       # strong: a leased page must
        return live                          # never depend on gc

    async def _swap_document(self, live: LiveDocument, ref: Reference,
                             nav: Any) -> LiveDocument:
        page = live._page
        if page is None:
            raise RuntimeError(
                "this LiveDocument was released or navigated away")
        self._detach(live._attached)
        if live._routing is not None:
            live._routing.cancel()
        document_id = uuid4().hex
        events: list[Any] = []
        routing = self._route_events(document_id, events)
        attached = self._attach("page", page, document_id=document_id,
                                session_id=live.session_id)
        await self._run_attach_async(attached)
        try:
            response = await nav(page)
        except Exception as exc:
            self._detach(attached)
            routing.cancel()
            raise FetchError(f"navigate {ref.url}: {exc}") from exc
        new = await self._wrap_live_page(
            Reference.from_url(page.url), document_id, page, response,
            live._lease, live._session, attached, events, routing)
        live._page = None
        live._lease = None
        live._attached = []
        self._live.pop(live.id, None)
        return new

    def _navigate(self, live: LiveDocument, ref: Reference, *,
                  headers: dict[str, str] | None,
                  wait_until: str) -> LiveDocument:
        async def nav(page: Any) -> Any:
            if headers:
                await page.set_extra_http_headers(headers)
            return await page.goto(ref.url, wait_until=wait_until)
        return self._ensure_loop().run(self._swap_document(live, ref, nav))

    def _history(self, live: LiveDocument,
                 direction: Literal["back", "forward"]) -> LiveDocument:
        async def nav(page: Any) -> Any:
            move = page.go_back if direction == "back" else page.go_forward
            return await move()
        ref = Reference.from_url(live.final_url or live.url)
        return self._ensure_loop().run(self._swap_document(live, ref, nav))

    async def _release_live(self, live: LiveDocument) -> None:
        if live._routing is not None:
            live._routing.cancel()
            live._routing = None
        if live._attached:
            self._detach(live._attached)
            live._attached = []
        if live._lease is not None:
            await self.pool._release(live._lease)
            live._lease = None
        live._page = None
        self._live.pop(live.id, None)

    async def _fetch(self, ref: Reference, *, optional: bool,
                     session: Any = None) -> Document:
        session = session or ref._session
        if session is not None:
            session.check()
        document_id = uuid4().hex
        routed: list[Any] = []
        subscription = self.bus.subscribe("", routed.append,
                                          document_id=document_id)
        headers = {**self.default_headers,
                   **(session.headers if session else {}), **ref.headers}
        cookies = {**(session.cookies if session else {}), **ref.cookies}
        timeout = self.timeout
        if session is not None and session.timeout is not None:
            timeout = session.timeout
        lease = await self.pool._acquire("http", session=session)
        try:
            attached = self._attach(
                "transport", lease._client, document_id=document_id,
                session_id=session.id if session else None)
            try:
                response = await engine_http.request(
                    lease._client, ref, headers=headers, cookies=cookies,
                    timeout=timeout, retries=self.retries)
            finally:
                self._detach(attached)
        except httpx.TransportError as exc:
            subscription.cancel()
            if optional:
                return self._build_document(ref, document_id, None, routed,
                                            session)
            raise FetchError(f"{ref.method.upper()} {ref.url}: {exc}") from exc
        finally:
            await self.pool._release(lease)
        subscription.cancel()
        if session is not None:
            for hop in (*response.history, response):
                session.cookies.update(dict(hop.cookies))
        document = self._build_document(ref, document_id, response, routed,
                                        session)
        if not optional and not document.ok:
            raise FetchError(
                f"{ref.method.upper()} {ref.url} -> {document.status_code}",
                document=document)
        return document

    def _build_document(self, ref: Reference, document_id: str,
                        response: httpx.Response | None,
                        routed: list[Any], session: Any = None) -> Document:
        fields = {name: getattr(ref, name) for name in Reference.model_fields}
        if response is None:
            document = Document(**fields, id=document_id, status_code=0)
        else:
            content_type = response.headers.get("content-type")
            document = Document(
                **fields,
                id=document_id,
                kind=engine_http.sniff_kind(content_type, response.content),
                content=response.content,
                status_code=response.status_code,
                response_headers=dict(response.headers),
                encoding=engine_http.charset_of(content_type),
                final_url=str(response.url),
                elapsed=response.elapsed.total_seconds(),
            )
        document._client = self
        document._session = session
        if session is not None:
            document.session_id = session.id
        document.events.extend(routed)
        self._documents[document_id] = weakref.ref(document)
        self._attach("document", document, document_id=document_id)
        return document

    # -- registries ----------------------------------------------------------
    def document(self, document_id: str) -> Document | None:
        found = self._documents.get(document_id)
        return found() if found is not None else None

    # -- sessions -------------------------------------------------------------
    def session(self, **overrides: Any) -> "Session":
        from .session import Session
        import time as _time
        sess = Session(id=uuid4().hex, status="running", **overrides)
        if sess.ttl is not None:
            sess.expires_at = _time.time() + sess.ttl
        sess._client = self
        self._sessions[sess.id] = sess
        self._attach("session", sess, session_id=sess.id)
        return sess

    # -- pagination (static; live pagination lands in M4) ---------------------
    def _paginate(self, first: Document, on: Any, until: Any,
                  limit: int | None, offset: int, resume: Reference | None,
                  prefetch: int, session: Any):
        loop = self._ensure_loop()

        def next_from(doc: Document, iterator: Any, first_ref: Reference):
            if callable(on):
                return on(doc)
            if isinstance(on, str):
                node = doc.select(on, optional=True)
                return node.attr("href", optional=True) if node is not None else None
            item = next(iterator, None)
            if item is None:
                return None
            if isinstance(item, Reference):
                return item
            if isinstance(item, dict):
                return first_ref.with_params(**{k: str(v) for k, v in item.items()})
            raise TypeError(
                "iterable pagination items must be dicts (query params) or "
                f"References, got {type(item).__name__}")

        def stops(doc: Document) -> bool:
            if until is None:
                return False
            if callable(until):
                return bool(until(doc))
            return doc.select(until, optional=True) is not None

        async def pages():
            iterator = iter(on) if not (callable(on) or isinstance(on, str)) else None
            first_ref = Reference(
                **{name: getattr(first, name) for name in Reference.model_fields})
            if resume is not None:
                current = await self._fetch(resume, optional=False,
                                            session=session)
            else:
                current = first
            fetched, index = 1, 0
            while True:
                if index >= offset:
                    yield current
                index += 1
                if limit is not None and fetched >= limit:
                    break
                next_ref = next_from(current, iterator, first_ref)
                if next_ref is None:
                    break
                doc = await self._fetch(next_ref, optional=False,
                                        session=session)
                fetched += 1
                if stops(doc):
                    break
                current = doc

        return loop.stream(pages(), buffer=max(1, prefetch))

    def release(self, doc: Any) -> None:
        """Return a live page's lease to the pool."""
        if not isinstance(doc, LiveDocument):
            raise TypeError("release() takes a LiveDocument")
        self._ensure_loop().run(self._release_live(doc))

    def _teardown_session(self, session: Any) -> None:
        """Release the session's pages and persist its browser storage."""
        if self._closed or self._loop is None or self._loop.closed:
            return
        for live in [d for d in self._live.values()
                     if d.session_id == session.id]:
            self._loop.run(self._release_live(live))
        if self._browser is not None:
            self._loop.run(self._browser.close_session_context(session))

    def execute(self, plan: Any, context: Any, *, stream: bool = False) -> Any:
        """Compile and run a lazy plan against ``context`` via the Executor."""
        return self._executor().run(plan, context, stream=stream)

    def _executor(self) -> Any:
        if self._exec is None:
            from .lazy.executor import Executor
            self._exec = Executor()
            self._exec._client = self
        return self._exec


# --------------------------------------------------------------------------- #
# Process-wide default client (ISSUES #17)
# --------------------------------------------------------------------------- #

_default: WebClient | None = None
_default_lock = threading.Lock()


def default_client() -> WebClient:
    """Lazily-created process default; recreated after close; closed
    best-effort at interpreter exit."""
    global _default
    with _default_lock:
        if _default is None or _default._closed:
            _default = WebClient()
        return _default


@atexit.register
def _close_default() -> None:
    with _default_lock:
        if _default is not None and not _default._closed:
            try:
                _default.close()
            except Exception:  # best-effort teardown only
                pass
