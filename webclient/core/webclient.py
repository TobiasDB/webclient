"""WebClientCore: the engine (pool, bus, plugins, loop, name scopes) and
all resolve/fetch/execute logic. The shallow ``WebClient`` facade
(webclient/webclient.py) forwards to it; every internal object's ``_client``
points here."""
from __future__ import annotations

import atexit
import logging
import threading
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Literal, Sequence, overload
from urllib.parse import quote_plus
from uuid import uuid4

if TYPE_CHECKING:
    from ..session import Session

import httpx
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr
from typing_extensions import Self

from ..engine import http as engine_http
from ..engine.browser import BrowserHost
from ..engine.loop import EngineLoop
from ..events import EventBus, EventRegistry
from ..document import (
    Document,
    apply_status,
    FetchError,
    HttpMethod,
    Reference,
    Script,
    WebBase,
)
from .base import EngineCore, NameScope, now
from ..plugins.base import Plugin, Renderer, Surface, SurfaceKind
from ..plugins.network import HttpNetworkPlugin
from ..plugins.page import PageConsolePlugin, PageDomPlugin, PageNetworkPlugin
from ..pool import ClientPool

logger = logging.getLogger("webclient")


class Proxy(BaseModel):
    """Upstream proxy config (used by a Session / the client proxy pool)."""

    url: str
    username: str | None = None
    password: str | None = None

    @property
    def authenticated_url(self) -> str:
        if self.username is None:
            return self.url
        scheme, _, rest = self.url.partition("://")
        auth = self.username + (f":{self.password}" if self.password else "")
        return f"{scheme}://{auth}@{rest}"


class SearchEngine(BaseModel):
    """How to drive one search engine: a URL template (``{q}`` = the escaped
    query) and the selectors for a result, its title and its link."""

    url: str = "https://duckduckgo.com/html/?q={q}"
    result: str = ".result"
    title: str = ".result__a"
    link: str = ".result__a"


def _core_plugins() -> list[Plugin]:
    return [HttpNetworkPlugin(), PageNetworkPlugin(), PageConsolePlugin(),
            PageDomPlugin()]


class WebClientCore(EngineCore, BaseModel):
    """The engine and lifecycle root. Objects bind their ``_client`` to a
    WebClientCore; the WebClient facade forwards to it. Loop lifecycle
    (``_ensure_loop``/``close``) is shared with the remote core via
    :class:`EngineCore`."""

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
    names_cap: int = 1024            # client-scope retention (sessions: unbounded, dropped on close)

    # owned infrastructure (default_factory: these hold runtime state)
    pool: ClientPool = Field(default_factory=ClientPool)
    bus: EventBus = Field(default_factory=EventBus)
    registry: EventRegistry = Field(default_factory=EventRegistry)
    plugins: list[Plugin] = Field(default_factory=list)

    _loop: EngineLoop | None = PrivateAttr(default=None)
    _loop_lock: Any = PrivateAttr(default_factory=threading.Lock)
    _scope: Any = PrivateAttr(default=None)
    _scope_seq: int = PrivateAttr(default=0)
    _sessions: dict[str, Any] = PrivateAttr(default_factory=dict)
    _live: dict[str, Any] = PrivateAttr(default_factory=dict)  # strong refs
    _browser: Any = PrivateAttr(default=None)
    _render_table: dict[tuple[str, str], Renderer] = PrivateAttr(default_factory=dict)
    _closed: bool = PrivateAttr(default=False)

    def model_post_init(self, __context: Any) -> None:
        self.pool._owner = self
        self._scope = NameScope(0, self.names_cap)
        for plugin in _core_plugins():
            self.use(plugin)

    # -- lifecycle (close / _ensure_loop / __enter__ come from EngineCore) ----
    async def aclose(self) -> None:
        """Async teardown: sessions first (persists browser storage_state),
        then any remaining live pages, the browser, and the pool. The core
        does the work; the sync ``close`` is the only bridge."""
        for sess in list(self._sessions.values()):
            try:
                await self._ateardown_session(sess)
            except Exception:
                pass
            sess.status = "closed"
        for live in list(self._live.values()):
            try:
                await self._release_live(live)
            except Exception:
                pass
        if self._browser is not None:
            await self._browser.aclose()
        await self.pool._aclose()

    def _finalize(self) -> None:
        for sess in self._sessions.values():
            sess.status = "closed"

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

    # -- resolve (the one lifecycle decision point) --------------------------
    async def resolve(self, ref: Reference, *, browser: bool = False,
                      session: Any = None, optional: bool = False,
                      **options: Any) -> Document:
        """Build a Document from a Reference: choose the http or browser path,
        set ok/error, and replay the reference's recorded action chain
        (Decision 11) so re-resolution reproduces state. ``optional=True``
        returns a not-ok Document instead of raising on failure."""
        ref.options = {"browser": browser, **options}
        if browser:
            doc = await self._fetch_browser(
                ref, session=session, scripts=options.get("scripts"),
                wait_until=options.get("wait_until", "load"), optional=optional)
            if ref.actions:
                await doc._core.dispatch("replay", list(ref.actions))
            return doc
        return await self._fetch(ref, optional=optional, session=session)

    # -- browser path (M4) ----------------------------------------------------
    def _browser_host(self) -> BrowserHost:
        if self._browser is None:
            self._browser = BrowserHost(headless=self.headless)
        return self._browser

    async def _fetch_browser(self, ref: Reference, *, session: Any,
                             scripts: Sequence[Script] | None,
                             wait_until: str, optional: bool) -> Document:
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
        scope = self._scope_for(session)
        scope.add(ref)
        document_id = scope.reserve("doc")
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
                              events: list[Any], routing: Any) -> Document:
        live = Document(
            id=document_id, name=document_id, root=ref.name or None,
            url=ref.url, final_url=page.url, kind="html",
            content=(await page.content()).encode(),
            status_code=response.status if response is not None else 200,
            response_headers=dict(response.headers) if response is not None else {},
            options=dict(ref.options),
        )
        apply_status(live)
        live._client = self
        live._session = session
        live._core.page = page
        live._core.lease = lease
        live._core.attached = attached
        live._core.routing = routing
        live.events.extend(events)           # events captured during goto
        # keep routing appending to the live document's own list
        routing.cancel()
        live._core.routing = self._route_events(document_id, live.events)
        if session is not None:
            live.session_id = session.id
            for cookie in await page.context.cookies():
                session.cookies[cookie["name"]] = cookie["value"]
        self._scope_for(session).add(live)
        self._live[document_id] = live       # strong: a leased page must
        return live                          # never depend on gc

    async def _release_live(self, live: Document) -> None:
        if live._core.routing is not None:
            live._core.routing.cancel()
            live._core.routing = None
        if live._core.attached:
            self._detach(live._core.attached)
            live._core.attached = []
        if live._core.lease is not None:
            await self.pool._release(live._core.lease)
            live._core.lease = None
        live._core.page = None
        self._live.pop(live.id, None)

    async def _fetch(self, ref: Reference, *, optional: bool,
                     session: Any = None) -> Document:
        session = session or ref._session
        if session is not None:
            session.check()
        scope = self._scope_for(session)
        scope.add(ref)
        document_id = scope.reserve("doc")
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
        identity = dict(id=document_id, name=document_id,
                        root=ref.name or None, url=ref.url,
                        options=dict(ref.options))
        if response is None:
            document = Document(**identity, status_code=0)
        else:
            content_type = response.headers.get("content-type")
            document = Document(
                **identity,
                kind=engine_http.sniff_kind(content_type, response.content),
                content=response.content,
                status_code=response.status_code,
                response_headers=dict(response.headers),
                encoding=engine_http.charset_of(content_type),
                final_url=str(response.url),
                elapsed=response.elapsed.total_seconds(),
            )
        apply_status(document)
        document._client = self
        document._session = session
        if session is not None:
            document.session_id = session.id
        document.events.extend(routed)
        self._scope_for(session).add(document)
        self._attach("document", document, document_id=document_id)
        return document

    # -- name registry (Decision 9) ------------------------------------------
    def _scope_for(self, session: Any) -> NameScope:
        return session._scope if session is not None else self._scope

    def _lookup(self, name: str) -> Any:
        """Client scope, then every live session scope: a client sees its
        sessions' objects; a session sees only its own."""
        found = self._scope.get(name)
        if found is None:
            for sess in self._sessions.values():
                found = sess._scope.get(name)
                if found is not None:
                    break
        return found

    def document(self, name: str) -> Document | None:
        found = self._lookup(name)
        return found if isinstance(found, Document) else None

    def reference(self, name: str) -> Reference | None:
        found = self._lookup(name)
        return found if isinstance(found, Reference) and not isinstance(
            found, Document) else None

    # -- sessions -------------------------------------------------------------
    def session(self, **overrides: Any) -> "Session":
        from ..session import Session
        import time as _time
        sess = Session(id=uuid4().hex, status="running", **overrides)
        if sess.ttl is not None:
            sess.expires_at = _time.time() + sess.ttl
        sess._client = self
        self._scope_seq += 1
        sess._scope = NameScope(self._scope_seq)
        self._sessions[sess.id] = sess
        self._attach("session", sess, session_id=sess.id)
        return sess

    # -- lease release (sync bridges over async core primitives) --------------
    def release(self, doc: Any) -> None:
        """Return a live page's lease to the pool (sync bridge)."""
        if getattr(doc, "_core", None) is None or doc._core.page is None:
            return                       # already released or never live
        self._ensure_loop().run(self._release_live(doc))

    async def _ateardown_session(self, session: Any) -> None:
        """Release the session's pages and persist its browser storage."""
        for live in [d for d in self._live.values()
                     if d.session_id == session.id]:
            await self._release_live(live)
        if self._browser is not None:
            await self._browser.close_session_context(session)
        session._scope.clear()
        self._sessions.pop(session.id, None)

    def _teardown_session(self, session: Any) -> None:
        """Sync bridge for ``Session.close()``."""
        if self._closed or self._loop is None or self._loop.closed:
            return
        self._loop.run(self._ateardown_session(session))

    # -- execute (the other primitive; async, like resolve) ------------------
    async def execute(self, expr: Any, context: Any = None) -> Any:
        """Evaluate a lazy expression against ``context`` (none needed for a
        plan rooted at ``Reference(url)``)."""
        from ..lazy import executor
        return await executor.evaluate(expr, context, client=self)

    def astream(self, expr: Any, context: Any = None) -> Any:
        """The streaming form: an async iterator of rows as they complete."""
        from ..lazy import executor
        return executor.stream(expr, context, client=self)
