"""The engine: ``WebClientCore`` (pool, bus, plugins, loop, name scopes +
all resolve/fetch/execute logic), the plan-building facades ``WebClient`` /
``AsyncWebClient`` over it, and ``Session``. Everything the user drives folds
into this one module (PLAN §9); every internal object's ``_client`` points at
a ``WebClientCore``."""
from __future__ import annotations

import atexit
import logging
import threading
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Literal, Sequence, cast, overload
from urllib.parse import quote_plus
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from ..stubs import Lazy, LazyCollection, LazyDocument, LazyReference

import httpx
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr
from typing_extensions import Self

from ..engine import http as engine_http
from ..engine.browser import BrowserHost
from ..engine.loop import EngineLoop
from ..events import EventBus, EventRegistry
from . import expr as _lz
from .expr import Expr, Plan
from .document import (
    Document,
    apply_status,
    FetchError,
    HttpMethod,
    Reference,
    Script,
    WebBase,
)
from .base import RAISE, RETURN, EngineCore, NameScope, now
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
        from . import executor
        return await executor.evaluate(expr, context, client=self)

    def astream(self, expr: Any, context: Any = None) -> Any:
        """The streaming form: an async iterator of rows as they complete."""
        from . import executor
        return executor.stream(expr, context, client=self)


# ========================================================================= #
# Plan builders + the client facades (merged from facade.py + client.py)
# ========================================================================= #

# -- plan builders (shared by the facades and Session) ---------------------- #

def fetch_expr(*, browser: bool = False, optional: bool = False,
               **options: Any) -> Any:
    """``ref.resolve(...)``. The explicit error policy makes a plan (RETURN by
    default) still raise on a hard fetch unless the caller opted out."""
    return _lz.ref.resolve(browser=browser, optional=optional,
                           error=RETURN if optional else RAISE, **options)


def search_expr(engine: Any, *, limit: int = 5) -> Any:
    """Resolve the search page, then project a title/url row per result."""
    doc = _lz.doc
    return (_lz.ref.resolve().select_all(engine.result, limit=limit)
            .extract(title=doc.select(engine.title).attr("text"),
                     url=doc.select(engine.link).attr("href"))
            .project())


def default_engine() -> Any:
    return SearchEngine()


def run_on_core(core: Any, expr: Any, context: Any = None, *,
                stream: bool = False) -> Any:
    """Bridge a lazy expression onto the core's engine loop and block (sync)."""
    loop = core._ensure_loop()
    if stream:
        pool = getattr(core, "pool", None)
        return loop.stream(core.astream(expr, context),
                           buffer=max(1, getattr(pool, "max_http", 8)))
    return loop.run(core.execute(expr, context))


# -- the shared facade ------------------------------------------------------ #

class _Facade:
    """The user surface: the same plan builders over any execution. A subclass
    supplies ``_core``/``ref``/``_run`` (and ``session`` for a session-bound
    surface)."""

    _core: Any

    def ref(self, url: str, method: str = "get", **kwargs: Any) -> Any:
        raise NotImplementedError

    def _run(self, expr: Any, context: Any = None, *, stream: bool = False) -> Any:
        raise NotImplementedError

    def _bind(self, ref: Any, session: Any = None) -> Any:
        """Make a caller-supplied reference resolvable against this client;
        remote refs already carry what they need. Overridden locally."""
        return ref

    def _context(self, ref: Any, session: Any = None, **kwargs: Any) -> Any:
        """A real ``Reference`` whose request spec roots the lazy plan
        (``_rooted``). A string becomes ``Reference.from_url``; a given
        Reference is used as-is. (The public ``ref`` is lazy; this is internal
        and stays a real Reference so the plan can embed its source.)"""
        from .document import Reference
        return Reference.from_url(ref, **kwargs) if isinstance(ref, str) else ref

    def execute(self, expr: Any, context: Any = None, *,
                stream: bool = False) -> Any:
        """Run a lazy expression. The precise ``Lazy[T] -> T`` typing lives on
        the concrete clients (``WebClient`` sync, ``AsyncWebClient`` awaitable),
        since their return shapes differ; here it stays ``Any``. ``stream=True``
        yields rows as they land."""
        return self._run(expr, context, stream=stream)

    def _rooted(self, context: Any, **resolve_opts: Any) -> "LazyDocument":
        """A lazy Document expr: resolve ``context`` self-contained (its request
        spec is embedded in the plan), bound to this client's core -- so
        ``.collect()`` needs no separate context."""
        root = Expr(Plan(root="Reference", source=context.request_fields()),
                    self._core)
        return cast("LazyDocument", root.resolve(**resolve_opts))

    def fetch(self, ref: Any, *, browser: bool = False, session: Any = None,
              optional: bool = False, **options: Any) -> "LazyDocument":
        """Lazy (PLAN §8): a Document expr bound to this client; run with
        ``.collect()`` (sync), ``await ac.execute(...)`` (async), or
        ``wc.execute``. Was eager."""
        ctx = self._context(ref, session)
        return self._rooted(ctx, browser=browser, optional=optional,
                            error=RETURN if optional else RAISE, **options)

    def search(self, term: str, *, engine: Any = None, limit: int = 5,
               session: Any = None) -> "LazyCollection":
        """Lazy: a rows expr (a title/url record per result); run with
        ``.collect()``."""
        engine = engine or default_engine()
        ctx = self._context(engine.url.format(q=quote_plus(term)), session)
        rows = (self._rooted(ctx).select_all(engine.result, limit=limit)
                .extract(title=_lz.doc.select(engine.title).attr("text"),
                         url=_lz.doc.select(engine.link).attr("href")).project())
        return cast("LazyCollection", rows)

    def summary(self, url: str, *, browser: bool = False,
                session: Any = None, **reference_like: Any) -> Any:
        """Lazy: a compact title + markdown record; run with ``.collect()``."""
        ctx = self._context(url, session, **reference_like)
        return (self._rooted(ctx, browser=browser)
                .extract(url=_lz.doc.final_url, ok=_lz.doc.is_ok(),
                         title=_lz.doc.title, markdown=_lz.doc.render("markdown"))
                .project())


class _Client(_Facade):
    """A facade over a core backend: it owns (or is given) a core and forwards
    the core's lifecycle/registry surface (``session``/``document``/``use`` …).
    ``core`` defaults to a local :class:`WebClientCore`; pass ``core=`` to run
    against another backend (e.g. ``RemoteWebClientCore``)."""

    def __init__(self, core: Any = None, **policy: Any) -> None:
        object.__setattr__(self, "_core",
                           core if core is not None else WebClientCore(**policy))

    @property
    def core(self) -> Any:
        return self._core

    def ref(self, url: str, method: str = "get", **kwargs: Any) -> "LazyReference":
        """A LAZY reference root bound to this client's core: it records ops and
        runs on ``.collect()`` (or ``wc.execute``), on THIS client's core
        (PLAN §8 -- was eager). Build a plain request spec with
        ``Reference.from_url`` if you need to inspect ``.url``/``.path``."""
        from .document import HttpMethod
        from .expr import Expr, Plan
        spec = Reference.from_url(url, method=cast(HttpMethod, method),
                                 **kwargs).request_fields()
        return cast("LazyReference", Expr(Plan(root="Reference", source=spec), self._core))

    #: ``lazy`` is kept as an explicit alias of the (now lazy) ``ref``.
    lazy = ref

    def close(self) -> None:
        self._core.close()

    def __enter__(self) -> "_Client":
        return self

    def __exit__(self, *exc: object) -> None:
        self._core.close()

    def __getattr__(self, name: str) -> Any:
        # session / document / reference / release / use / pool / bus / …
        if name == "_core":
            raise AttributeError(name)
        return getattr(object.__getattribute__(self, "_core"), name)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(core={self._core!r})"


class AsyncWebClient(_Client):
    """The async client -- the core's native form. ``await ac.fetch(url)`` /
    ``await ac.execute(plan, ctx)`` run the async core directly on the
    caller's event loop (no engine thread, no bridge);
    ``execute(..., stream=True)`` is an async iterator of rows."""

    def _run(self, expr: Any, context: Any = None, *,
             stream: bool = False) -> Any:
        if stream:
            return self._core.astream(expr, context)    # async iterator
        return self._core.execute(expr, context)        # coroutine

    @overload  # async: materialising awaits to T
    def execute[T](self, expr: "Lazy[T]", context: Any = ...) -> "Coroutine[Any, Any, T]": ...
    @overload
    def execute(self, expr: Any, context: Any = ..., *, stream: bool = ...) -> Any: ...
    def execute(self, expr: Any, context: Any = None, *,
                stream: bool = False) -> Any:
        """Await to materialise: ``await ac.execute(ac.fetch(u))`` -> ``Document``
        (the ``Lazy[T]`` bridge, awaited). ``stream=True`` is an async iterator."""
        return self._run(expr, context, stream=stream)

    async def __aenter__(self) -> "AsyncWebClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        if not self._core._closed:
            await self._core.aclose()
            self._core._closed = True


class WebClient(_Client):
    """The synchronous client: the one surface that bridges the async core
    onto a dedicated engine loop and blocks for the result. ``wc.core`` is
    the async engine underneath."""

    def _run(self, expr: Any, context: Any = None, *,
             stream: bool = False) -> Any:
        return run_on_core(self._core, expr, context, stream=stream)

    @overload
    def execute[T](self, expr: "Lazy[T]", context: Any = ...) -> T: ...
    @overload
    def execute(self, expr: Any, context: Any = ..., *, stream: bool = ...) -> Any: ...
    def execute(self, expr: Any, context: Any = None, *,
                stream: bool = False) -> Any:
        """Run a lazy expression and materialise it: a lazy tier (``LazyDocument``
        / ``LazyField[str]`` / …) comes back as its model (``Document`` /
        ``Field[str]`` / …) via the ``Lazy[T]`` bridge. ``stream=True`` yields
        rows as they land (typed ``Any``)."""
        return self._run(expr, context, stream=stream)


_default: WebClient | None = None
_default_lock = threading.Lock()


def default_client() -> WebClient:
    """Lazily-created process default; recreated after close; closed
    best-effort at interpreter exit."""
    global _default
    with _default_lock:
        if _default is None or _default._core._closed:
            _default = WebClient()
        return _default


@atexit.register
def _close_default() -> None:
    with _default_lock:
        if _default is not None and not _default._core._closed:
            try:
                _default.close()
            except Exception:  # best-effort teardown only
                pass


# ========================================================================= #
# Session (merged from session.py)
# ========================================================================= #

class Session(BaseModel):
    id: str = ""
    status: Literal["pending", "running", "expired", "closed"] = "pending"
    ttl: float | None = None
    keep_alive: bool = False
    expires_at: float | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    cookies: dict[str, str] = Field(default_factory=dict)
    proxy: Proxy | None = None
    storage_state: dict[str, Any] | None = None
    timeout: float | None = None

    _client: Any = PrivateAttr(default=None)   # owning WebClient
    _scope: Any = PrivateAttr(default=None)    # NameScope: this session's names

    def ref(self, url: str, method: HttpMethod = "get",
            **kwargs: Any) -> "LazyReference":
        """A LAZY reference root scoped to this session: records ops and runs on
        ``.collect()`` (or ``session.execute``), resolving within this session
        (PLAN §8 -- was eager). The plan carries this session's id."""
        from .expr import Expr, Plan
        spec = Reference.from_url(url, method=method, **kwargs).request_fields()
        return cast("LazyReference", Expr(
            Plan(root="Reference", source=spec, session_id=self.id), self._client))

    def document(self, name: str) -> Document | None:
        found = self._scope.get(name) if self._scope is not None else None
        return found if isinstance(found, Document) else None

    def reference(self, name: str) -> Reference | None:
        found = self._scope.get(name) if self._scope is not None else None
        return found if isinstance(found, Reference) and not isinstance(
            found, Document) else None

    # -- client surface bound to this session (WebSession, PLAN §5d) ---------
    # Same plan builders as the WebClient facade, run on the core with this
    # session as the resolution context (see webclient.client).
    def fetch(self, ref: "Reference | str", *, browser: bool = False,
              optional: bool = False, **options: Any) -> "LazyDocument":
        """Lazy resolve within this session: a Document expr (session-scoped);
        run with ``.collect()`` / ``session.execute`` (PLAN §8 -- was eager)."""
        from .base import RAISE, RETURN
        from .expr import Expr, Plan
        r = ref if isinstance(ref, Reference) else Reference.from_url(ref)
        root = Expr(Plan(root="Reference", source=r.request_fields(),
                         session_id=self.id), self._client)
        return cast("LazyDocument", root.resolve(
            browser=browser, optional=optional,
            error=RETURN if optional else RAISE, **options))

    @overload
    def execute[T](self, expr: "Lazy[T]", context: Any = ...) -> T: ...
    @overload
    def execute(self, expr: Any, context: Any = ..., *, stream: bool = ...) -> Any: ...
    def execute(self, expr: Any, context: Any = None, *,
                stream: bool = False) -> Any:
        """Run a lazy expression within this session (sync bridge); a lazy tier
        materialises to its model via the ``Lazy[T]`` bridge."""
        return run_on_core(self._client, expr, context, stream=stream)

    def search(self, term: str, *, engine: Any = None,
               limit: int = 5) -> list[dict[str, Any]]:
        """Run a search within this session and return result rows."""
        from urllib.parse import quote_plus
        eng = engine or default_engine()
        rows = self.execute(search_expr(eng, limit=limit),
                            self.ref(eng.url.format(q=quote_plus(term))))
        return cast("list[dict[str, Any]]", rows)

    def check(self) -> None:
        """Raise unless the session is usable; lazily expires on ttl."""
        if self.expires_at is not None and time.time() > self.expires_at:
            if self.status == "running":
                self.status = "expired"
        if self.status in ("expired", "closed"):
            raise RuntimeError(f"session {self.id} is {self.status}")

    def close(self) -> None:
        if self.status == "closed":
            return
        if self._client is not None:
            try:
                self._client._teardown_session(self)
            except RuntimeError:
                pass                       # engine already stopped
        self.status = "closed"
