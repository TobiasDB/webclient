"""`WebClient` — session state, pooled dependencies, and resolving references
into Documents.

That is the whole job. It does not build pages, order subscriptions, parse
content or own an executor: a backing owns its resource, the pool owns the
transports, and the evaluator owns plans.
"""
from __future__ import annotations

import threading
import weakref
from typing import Any, Callable, Iterator, Sequence
from uuid import uuid4

from .backing import HttpBacking, StaticBacking, charset_of, sniff_kind
from .document import Document
from .engine import run_sync
from .errors import ResolveError, WebClientError
from .middleware import Middleware, Request, compose, default_stack
from .pool import ClientPool, PoolStats
from .reference import Proxy, Reference, Script
from .render import RendererRegistry, default_registry
from .session import Session
from .telemetry import Observers, Record, Telemetry


class _Core:
    """The async implementations. Reached as `wc.core` by callers already
    inside an event loop; the sync surface wraps these on the engine."""

    __slots__ = ("_wc",)

    def __init__(self, client: "WebClient") -> None:
        self._wc = client

    # -- resolve -------------------------------------------------------------
    async def resolve(self, target: "str | Reference", *, browser: bool = False,
                      session: Session | None = None, optional: bool = False,
                      wait_until: str = "load",
                      scripts: Sequence[Script] | None = None,
                      **options: Any) -> Document:
        wc = self._wc
        wc._check_open()
        reference = Reference.coerce(target)
        session = session or reference._session
        if session is not None:
            session.check()
        if browser:
            return await self._resolve_page(reference, session, wait_until,
                                            scripts, optional)
        return await self._resolve_http(reference, session, optional)

    async def _resolve_http(self, reference: Reference,
                            session: Session | None,
                            optional: bool) -> Document:
        wc = self._wc
        telemetry = Telemetry(wc.observers)
        request = Request(
            reference=reference,
            headers={**wc.default_headers,
                     **(session.headers if session else {}),
                     **reference.headers},
            cookies={**(session.cookies if session else {}),
                     **reference.cookies},
            timeout=(reference.timeout
                     or (session.timeout if session else None)
                     or wc.timeout),
            follow_redirects=reference.follow_redirects,
            telemetry=telemetry)

        lease = await wc.pool.acquire("http", session=session)
        try:
            send = compose(wc.middleware, _sender(lease))
            try:
                response = await send(request)
            except Exception as exc:
                document = self._failed_document(reference, session, telemetry,
                                                 str(exc))
                if optional:
                    return document
                raise ResolveError(
                    f"{reference.method.upper()} {reference.url}: {exc}",
                    document=document) from exc
        finally:
            await wc.pool.release(lease)

        if session is not None:
            session.cookies.update(dict(response.cookies))

        content_type = response.headers.get("content-type")
        backing = HttpBacking(
            response.content,
            sniff_kind(content_type, response.content),
            encoding=charset_of(content_type),
            status_code=response.status_code,
            final_url=str(response.url),
            response_headers=dict(response.headers),
            elapsed=response.elapsed.total_seconds(),
            message=None if response.is_success else
            f"HTTP {response.status_code}",
            telemetry=telemetry, request=reference)
        document = self._register(Document(backing=backing, request=reference,
                                           client=self._wc, session=session))
        if not optional and not (200 <= response.status_code < 300):
            raise ResolveError(
                f"{reference.method.upper()} {reference.url} -> "
                f"{response.status_code}", document=document,
                status_code=response.status_code)
        return document

    def _failed_document(self, reference: Reference, session: Session | None,
                         telemetry: Telemetry, message: str) -> Document:
        """A transport failure still gets a Document, so `otherwise(...)` can
        project `doc.status_code` (0) and `doc.message` uniformly."""
        backing = HttpBacking(b"", "binary", status_code=0,
                              final_url=reference.url, message=message,
                              telemetry=telemetry, request=reference)
        return self._register(Document(backing=backing, request=reference,
                                       client=self._wc, session=session))

    # -- browser -------------------------------------------------------------
    async def _resolve_page(self, reference: Reference,
                            session: Session | None, wait_until: str,
                            scripts: Sequence[Script] | None,
                            optional: bool) -> Document:
        from .page import PageBacking
        wc = self._wc
        lease = await wc.pool.acquire("page", session=session)
        telemetry = Telemetry(wc.observers)
        backing = PageBacking(lease.page, lease=lease, telemetry=telemetry,
                              request=reference, timeout=wc.timeout)
        try:
            await backing.prepare(session, [*wc.default_scripts,
                                            *(scripts or ())])
            status = await backing.goto(reference.url, wait_until=wait_until)
        except Exception as exc:
            await wc.pool.release(lease)
            if optional:
                return self._failed_document(reference, session, telemetry,
                                             str(exc))
            raise ResolveError(f"browser {reference.url}: {exc}") from exc

        document = self._register(Document(backing=backing, request=reference,
                                           client=self._wc, session=session))
        wc._live[document.id] = document          # a leased page never relies on gc
        if session is not None:
            await backing.sync_cookies(session)
        if not optional and not (200 <= status < 300):
            await self.release(document)
            raise ResolveError(f"browser {reference.url} -> {status}",
                               document=document, status_code=status)
        return document

    async def navigate(self, document: Document, reference: Reference, *,
                       wait_until: str = "load") -> Document:
        wc = self._wc
        backing = document._backing
        lease = getattr(backing, "lease", None)
        telemetry = Telemetry(wc.observers)
        moved = type(backing)(backing.page, lease=lease, telemetry=telemetry,
                              request=reference, timeout=wc.timeout)
        await document._freeze(reference.url)
        wc._live.pop(document.id, None)
        status = await moved.goto(reference.url, wait_until=wait_until)
        successor = self._register(Document(backing=moved, request=reference,
                                            client=wc,
                                            session=document._session))
        wc._live[successor.id] = successor
        if not (200 <= status < 300):
            successor._backing.message = f"HTTP {status}"
        return successor

    async def release(self, document: Document) -> None:
        """Return a live page to the pool. The Document keeps its snapshot."""
        backing = document._backing_obj
        wc = self._wc
        wc._live.pop(document.id, None)
        lease = getattr(backing, "lease", None) if backing else None
        if lease is None:
            return
        await document._freeze(None)
        await wc.pool.release(lease)

    # -- pagination ----------------------------------------------------------
    async def pages(self, start: Document, on: Any, *, until: Any = None,
                    limit: int | None = None, offset: int = 0,
                    session: Session | None = None) -> Any:
        """Yield documents from `start` onward. `on` is a selector whose match
        is followed, a callable returning the next Reference, or an iterable
        of references / query-param dicts."""
        iterator = None
        if not callable(on) and not isinstance(on, str):
            iterator = iter(on)
        first_reference = start.request or Reference.from_url(
            start._backing.final_url or "")
        current, index, fetched = start, 0, 1
        while True:
            if index >= offset:
                yield current
            index += 1
            if limit is not None and fetched >= limit:
                return
            following = self._next_reference(current, on, iterator,
                                             first_reference)
            if following is None:
                return
            current = await self.resolve(following, session=session)
            fetched += 1
            if _stops(current, until):
                return

    @staticmethod
    def _next_reference(document: Document, on: Any, iterator: Any,
                        first: Reference) -> Reference | None:
        if callable(on):
            following: Reference | None = on(document)
            return following
        if isinstance(on, str):
            node = document.select(on, optional=True)
            if node is None:
                return None
            return node.attr("href", optional=True)
        item = next(iterator, None)
        if item is None:
            return None
        if isinstance(item, Reference):
            return item
        if isinstance(item, str):
            return Reference.from_url(item)
        if isinstance(item, dict):
            return first.with_params(**{k: str(v) for k, v in item.items()})
        raise TypeError(
            "pagination items must be References, URLs or query-param dicts, "
            f"got {type(item).__name__}")

    # -- registry ------------------------------------------------------------
    def _register(self, document: Document) -> Document:
        self._wc._documents[document.id] = weakref.ref(document)
        return document


def _stops(document: Document, until: Any) -> bool:
    if until is None:
        return False
    if callable(until):
        return bool(until(document))
    return document.select(until, optional=True) is not None


def _sender(lease: Any) -> Any:
    """The terminal of the middleware chain: one actual HTTP round trip."""
    async def send(request: Request) -> Any:
        reference = request.reference
        client = lease.client
        for name, value in request.cookies.items():
            client.cookies.set(name, value)
        return await client.request(
            reference.method.upper(), reference.url,
            headers=request.headers or None,
            content=reference.body, json=reference.json_body,
            data=reference.form, timeout=request.timeout,
            follow_redirects=False)
    return send


# --------------------------------------------------------------------------- #
# WebClient
# --------------------------------------------------------------------------- #

class WebClient:
    """Lifecycle root: sessions, pooled dependencies, and `resolve`."""

    def __init__(self, *, timeout: float = 30.0, retries: int = 0,
                 verify_tls: bool = True, headless: bool = True,
                 default_headers: dict[str, str] | None = None,
                 proxy_pool: Sequence[Proxy] | None = None,
                 default_scripts: Sequence[Script] | None = None,
                 max_http: int = 10, max_pages: int = 4,
                 middleware: Sequence[Middleware] | None = None) -> None:
        self.timeout = timeout
        self.retries = retries
        self.verify_tls = verify_tls
        self.headless = headless
        self.default_headers = dict(default_headers or {})
        self.proxy_pool = list(proxy_pool or ())
        self.default_scripts = list(default_scripts or ())

        self.pool = ClientPool(max_http=max_http, max_pages=max_pages)
        self.pool.owner = self
        self.renderers: RendererRegistry = default_registry().child()
        self.observers = Observers()
        self.middleware: list[Middleware] = list(
            middleware if middleware is not None else default_stack(retries))
        self.core = _Core(self)

        self._documents: dict[str, Any] = {}
        self._live: dict[str, Document] = {}
        self._sessions: dict[str, Session] = {}
        self._browser_host: Any = None
        self._closed = False

    # -- resolving -----------------------------------------------------------
    def resolve(self, target: "str | Reference", *, browser: bool = False,
                session: Session | None = None, optional: bool = False,
                **options: Any) -> Document:
        """Turn a reference into a Document. The one verb for it."""
        return run_sync(self.core.resolve(target, browser=browser,
                                          session=session, optional=optional,
                                          **options))

    def ref(self, url: str, method: str = "get", **fields: Any) -> Reference:
        return Reference.from_url(url, method=method,  # type: ignore[arg-type]
                                  **fields).bind(self)

    def scrape(self, target: "str | Reference", *,
               formats: Sequence[str] = ("markdown",), browser: bool = False,
               session: Session | None = None,
               **options: Any) -> dict[str, Any]:
        """The one-shot convenience: resolve, render, release. Composition,
        not machinery."""
        document = self.resolve(target, browser=browser, session=session,
                                **options)
        try:
            out: dict[str, Any] = {
                "url": document.final_url.get(),
                "status": document.status_code.get(),
                "formats": {name: document.render(name).get()
                            for name in formats},
            }
            return out
        finally:
            if browser:
                self.release(document)

    def paginate(self, start: Document, on: Any, *, until: Any = None,
                 limit: int | None = None, offset: int = 0,
                 session: Session | None = None) -> Iterator[Document]:
        """Walk pages from `start`, one blocking iterator over the engine."""
        from .engine import portal
        source = self.core.pages(start, on, until=until, limit=limit,
                                 offset=offset, session=session)
        with portal().wrap_async_context_manager(
                _AsyncIteratorGuard(source)) as iterator:
            while True:
                item = portal().call(_anext, source)
                if item is _DONE:
                    return
                yield item

    # -- sessions ------------------------------------------------------------
    def session(self, **overrides: Any) -> Session:
        session = Session(**overrides)
        session._client = self
        self._sessions[session.id] = session
        return session

    def _close_session(self, session: Session) -> None:
        if self._closed:
            return
        for document in [d for d in list(self._live.values())
                         if d._session is session]:
            self.release(document)
        if self._browser_host is not None:
            run_sync(self._browser_host.close_context(session))
        self._sessions.pop(session.id, None)

    # -- registry / lifecycle -------------------------------------------------
    def document(self, document_id: str) -> Document | None:
        found = self._documents.get(document_id)
        return found() if found is not None else None

    def release(self, document: Document) -> None:
        run_sync(self.core.release(document))

    def on(self, record_type: type, handler: Callable[[Any], None]
           ) -> Callable[[], None]:
        """Stream telemetry records by class. No topics, no filters."""
        return self.observers.on(record_type, handler)

    def use(self, middleware: Middleware, *, first: bool = False) -> "WebClient":
        self.middleware.insert(0 if first else len(self.middleware) - 1,
                               middleware)
        return self

    def stats(self) -> PoolStats:
        return self.pool.stats()

    def _browser(self) -> Any:
        if self._browser_host is None:
            from .page import BrowserHost
            self._browser_host = BrowserHost(headless=self.headless)
        return self._browser_host

    def _check_open(self) -> None:
        if self._closed:
            raise WebClientError("this WebClient is closed")

    def close(self) -> None:
        if self._closed:
            return
        try:
            for document in list(self._live.values()):
                try:
                    self.release(document)
                except Exception:
                    pass
            if self._browser_host is not None:
                run_sync(self._browser_host.aclose())
            run_sync(self.pool.aclose())
        finally:
            self._closed = True
            for session in self._sessions.values():
                session.status = "closed"

    def __enter__(self) -> "WebClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return (f"WebClient(sessions={len(self._sessions)}, "
                f"live={len(self._live)}, "
                f"{'closed' if self._closed else 'open'})")


_DONE = object()


async def _anext(source: Any) -> Any:
    try:
        return await source.__anext__()
    except StopAsyncIteration:
        return _DONE


class _AsyncIteratorGuard:
    """Closes the generator when the consumer stops early, so a page lease is
    never held by an abandoned iterator."""

    def __init__(self, source: Any) -> None:
        self._source = source

    async def __aenter__(self) -> Any:
        return self._source

    async def __aexit__(self, *exc: object) -> None:
        await self._source.aclose()


# --------------------------------------------------------------------------- #
# Process default
# --------------------------------------------------------------------------- #

_default: WebClient | None = None
_default_lock = threading.Lock()


def default_client() -> WebClient:
    """Lazily created, recreated after close."""
    global _default
    with _default_lock:
        if _default is None or _default._closed:
            _default = WebClient()
        return _default
