"""WebClient: the engine core.

Holds the engine loop, a ``ClientPool`` (leasing http clients + browser pages),
the event bus, registered backings (``use``) and name scopes, and the machinery that drives
transport (``afetch``) and plan execution (``execute``/``aexecute``). Its
user-facing features are backings (``FetchBacking``: ref/fetch/summary --
:mod:`.fetch`); the ``WebClient`` / ``AsyncWebClient`` surface is a thin
sync/async/lazy interface over it. A remote backend is just a subclass that swaps
``execute`` for an HTTP round-trip -- so the surface is unchanged; only the core
differs.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Self, cast

from pydantic import PrivateAttr

from ...clients import (
    ClientPool,
    WaitConfig,
    WaitEvent,
)
from ...collection import Field
from ...errors import WebError, WebException, error_for
from ...events import EventBus
from ...models import NavigationEvent, NetworkEvent, PlanEvent
from ...query.executor import aevaluate, astream, evaluate
from ..document import Document
from ...resiliency import policy_headers
from ...signals import flags_from_response
from ..reference import Reference, from_url
from ..reference.models import ProxyPolicy, Resolve
from ..engine import Engine
from ..session_core import SessionCore
from ..web_core import Backing
from .fetch import FetchBacking
from .loop import EngineLoop
from .models import IWebClient
from .sitemap import SiteBacking

if TYPE_CHECKING:
    from ..crawl import Crawl, CrawlConfig, CrawlState
    from ..session import Session
    from ...surfaces.lazy import LazyWebClient


_MODES = ("never", "auto", "always")


#: transport-error fingerprints that usually mean a server refused a suspected bot at
#: the protocol layer (before any response) -- read as an anti-bot trigger under ``auto``.
_BOT_BLOCK_HINTS = (
    "http2", "http/2", "protocol error", "connection reset", "server disconnected",
    "connection closed", "econnreset",
)


def _looks_like_bot_block(error: Any) -> bool:
    """Whether a transport ``error`` looks like a protocol-level anti-bot block (an
    HTTP/2 protocol error, a reset/dropped connection) -- worth escalating rather than
    surfacing, since a real browser stack often gets through."""
    if error is None:
        return False
    msg = (getattr(error, "message", "") or "").lower()
    return any(hint in msg for hint in _BOT_BLOCK_HINTS)


def _browser_mode(browser: Any) -> str:
    """Normalise the ``browser`` kwarg to a tier: ``"never"`` (static only),
    ``"auto"`` (static first, escalate on the response's signals), or ``"always"``
    (straight to a browser). Accepts a bool, one of those strings, ``AUTO``, or a
    ``BrowserPolicy`` (its ``when``)."""
    if browser is True:
        return "always"
    if not browser:  # False / None
        return "never"
    if isinstance(browser, str):
        return browser if browser in _MODES else "never"
    when = getattr(browser, "when", None)  # a BrowserPolicy
    return when if when in _MODES else "auto"


def _wait_of(browser: Any, wait: "WaitConfig | None") -> "WaitConfig | None":
    """The browser-render wait config for this fetch. An explicit ``wait`` wins;
    otherwise a ``BrowserPolicy.wait_for`` selector (the existing policy hook) is
    honoured as a ``WaitEvent.SELECTOR`` wait; otherwise ``None`` (the client's
    default DOM settle)."""
    if wait is not None:
        return wait
    selector = getattr(browser, "wait_for", None)  # a BrowserPolicy
    if selector:
        return WaitConfig(event=WaitEvent.SELECTOR, selector=selector)
    return None




def _flag_reason(flag: Any, default: str) -> str:
    """The lead evidence line of a flag (its highest-confidence-first signal), or a
    default -- used to phrase the error when ``auto`` fails on a login wall."""
    sigs = getattr(flag, "signals", None) or []
    return sigs[0].reason if sigs else default


def _seed_urls(seeds: Any) -> list[str]:
    """Normalise crawl seeds -- a URL string, a Reference (surface or core), or a
    list / :class:`Collection` of either (so ``wc.crawl(wc.sitemap(url))`` composes) --
    to a list of URL strings."""
    from ...collection import Collection

    items = list(seeds) if isinstance(seeds, (list, tuple, Collection)) else [seeds]
    return [s if isinstance(s, str) else str(getattr(s, "url", s)) for s in items]


def _materialize(result: Any) -> Any:
    """A materialised plan result: a scalar leaf becomes a ``Field``; a surface
    or collection passes through."""
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


class WebClient(SessionCore, IWebClient):
    """The engine: its Core Fields (policy) + eager verbs come from the
    ``IWebClient`` model/interface it inherits (:mod:`.models`); this core adds the
    machinery (loop, ClientPool, bus, name scopes, transport + plan execution). Its
    user-facing verbs are backings (``FetchBacking``); a remote
    backend is just a subclass that swaps ``execute``, sessions a scoped subclass."""

    if TYPE_CHECKING:  # narrow WebCore.lazy (Any) to this core's lazy surface

        @property
        def lazy(self) -> "LazyWebClient": ...

    #: the shared transport/execution resources (loop / pool / bus / pacing / page
    #: scripts). The ROOT client owns one; a session borrows its parent's (its own
    #: stays ``None`` -- ``Session`` no-ops ``_init_transport``). See :class:`Engine`.
    _engine: Any = PrivateAttr(default=None)
    #: this session's own state (a ``SessionCore.store``); empty on the root client.
    _store: dict[str, Any] = PrivateAttr(default_factory=dict)
    _closed: bool = PrivateAttr(default=False)
    _scope: Any = PrivateAttr(default=None)  # the client's NameScope (000)
    _scope_counter: int = PrivateAttr(default=0)  # next session scope index
    _scope_lock: Any = PrivateAttr(default_factory=threading.Lock)  # guards ^
    _sessions: list[Any] = PrivateAttr(default_factory=list)  # sessions to close
    #: the dispatch mode -- an instance switch, not a subclass. ``"sync"`` blocks
    #: IO on a background engine loop; ``"async"`` is loop-native (IO runs on the
    #: caller's loop, awaited); ``"remote"`` turns every op into an API call. Every
    #: core reads its client's mode via ``WebCore._dispatch_mode``; the surface is
    #: the same core typed through the eager / ``Async*`` stubs. ``async_client()``
    #: sets ``"async"``; ``RemoteWebClientCore`` sets ``"remote"``.
    _mode: str = PrivateAttr(default="sync")

    @property
    def core(self) -> Self:
        """The engine core. The eager surface IS the core, so ``wc.core is wc`` --
        kept for call sites (and remote parity) that reach for ``.core``."""
        return self

    @property
    def bus(self) -> EventBus:
        return self._the_engine().bus

    def model_post_init(self, _ctx: Any) -> None:
        self._scope = NameScope(0, cap=self.names_cap)
        self._init_transport()

    def _init_transport(self) -> None:
        """Create this client's shared :class:`Engine` (transport pool + loop + bus +
        pacing). A ``Session`` overrides this to a no-op so it borrows the parent's
        engine instead of building its own."""
        self._engine = Engine(self.browser_config)

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

    BACKINGS: ClassVar[tuple[Backing, ...]] = (
        FetchBacking(),
        SiteBacking(),
    )

    # -- loop / lifecycle ----------------------------------------------------
    def loop(self) -> EngineLoop:
        return cast(EngineLoop, self._the_engine().loop())

    def bridge(self, coro: Any) -> Any:
        """Run an IO coroutine under this client's dispatch mode -- the one place
        the sync / async / on-loop distinction lives, so every IO backing
        (``resolve`` / ``fetch`` / ``summary``) is dispatcher-agnostic:

        - async client: loop-native -- hand the coroutine straight back so the
          caller ``await``s it on their own loop (this client's pool + pages bind
          to that loop; no engine-loop thread is ever started);
        - on the engine-loop thread (a sync client's executor is already awaiting
          there): hand the coroutine straight back;
        - sync caller: block on the background engine loop.

        Only the *sync* client uses the engine loop (a submit-and-wait pool) to
        drive async IO from blocking code; the async client owns its IO on the
        caller's loop."""
        if self._mode == "async":
            return coro
        loop = self.loop()
        if loop.on_loop_thread():
            return coro
        return loop.run(coro)

    @property
    def pool(self) -> ClientPool:
        """The transport-lease pool (http clients + browser pages)."""
        return self._the_engine().pool

    def close(self) -> None:
        if self._closed:
            return
        if self._engine is not None:  # a session borrows its parent's engine (its own is None)
            self._engine.close()  # tear down the transport pool + engine loop
        for session in self._sessions:  # cascade to sessions
            session.status = "closed"
        self._closed = True

    # -- context manager: a core IS the eager client (``with WebClient() ...``) --
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- async context manager (``async with AsyncWebClient() ...``). The async
    # client is the same core in ``"async"`` mode; close its loop-native pool on
    # the caller's loop (a sync client has no caller-loop pool -- close in a thread).
    async def aclose(self) -> None:
        if self._closed:
            return
        if self._mode == "async":
            if self._engine is not None:  # a session borrows its parent's engine
                await self._engine.aclose_async()  # close the pool loop-natively
            for session in self._sessions:  # cascade to sessions
                session.status = "closed"
            self._closed = True
            return
        import asyncio

        await asyncio.to_thread(self.close)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _host_blocked(self, ref: Reference) -> bool:
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

    # -- transport (machinery): resolve a Reference -> Document ------
    async def _pace(self, host: str) -> None:
        """Politeness: keep at least ``min_interval`` seconds between requests to
        ``host``. The schedule lives on the shared ENGINE (a session paces against its
        parent's), so N sessions on one engine honour ONE per-host rate limit rather
        than each keeping an independent schedule."""
        owner = getattr(self, "_parent", None) or self
        await owner._the_engine().pace(host, owner.min_interval)

    async def _afetch_once(
        self, ref: Reference, headers: dict[str, str]
    ) -> "tuple[Document, Any]":
        """One transport attempt: lease an http client from the pool and let it do
        the fetch (the client owns request + response interpretation). Binds the
        resulting document to this core; never raises, never registers -- the
        caller (``afetch``) retries, then registers/raises the final doc."""
        async with await self.pool.lease("http") as lease:
            client = cast(Any, lease.client)  # the leased HTTPXClient (subclass)
            doc, resp = await client.fetch(
                ref, headers=headers, cookies=ref.cookies, timeout=self.timeout
            )
        doc._client = self
        return doc, resp

    async def afetch(
        self,
        ref: Reference,
        *,
        optional: bool = False,
        browser: Any = False,
        resolve: Any = None,
        keep_alive: "bool | float" = False,
        wait: "WaitConfig | None" = None,
    ) -> Document:
        """Resolve ``ref`` into a document over a leased transport (http) or a
        browser page. ``browser`` picks the tier: ``False``/``"never"`` = static
        only, ``True``/``"always"`` = straight to a browser, ``"auto"`` (or a
        ``BrowserPolicy(when="auto")``) = static first, escalating to a browser
        render only when the page is JS-gated (the Crawlee adaptive rule). A
        retriable failure is retried up to ``retries`` times with exp backoff.
        ``resolve`` (a :class:`Resolve` bundle) overrides the client's own policy
        for this fetch -- its rate/retry/proxy concerns are declared to a downstream
        proxy service, and its ``retry.max`` bounds the local retry loop.
        ``keep_alive`` marks a browser page the CALLER owns (a plan won't
        auto-release it); a number keeps it with a TTL (auto-released after N
        seconds as a safety net). ``wait`` (a :class:`~webclient.clients.WaitConfig`)
        chooses the browser-render wait strategy + timeout behaviour; ``None`` uses a
        ``BrowserPolicy.wait_for`` selector when given, else the default DOM settle."""
        import asyncio

        pol = resolve if resolve is not None else self.resolve
        max_retries = pol.retry.max if pol is not None else self.retries
        mode = _browser_mode(browser)
        wait = _wait_of(browser, wait)
        if self.block_private_hosts and await self._host_blocked(ref):
            doc = Document(
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
        if mode == "always":
            try:
                return await self._alive(ref, keep_alive=keep_alive, wait=wait)
            except WebException:
                raise
            except Exception as exc:  # a render/launch failure
                if not optional:  # loud by default -- a browser crawl is optional=True
                    raise
                doc = Document(
                    url=ref.dispatch("url"), status_code=0,
                    error=WebError(type="BrowserError", message=str(exc)),
                )
                doc._client = self
                self._register(doc, ref)
                return doc
        # The X-WebClient-* policy headers are read by OUR special proxy / unblocker service --
        # engaged via a ``ProxyPolicy`` (``Resolve.proxy``). They must reach ONLY that service:
        # on a direct connection, or through a FIXED third-party proxy (``BrowserConfig.proxy``,
        # which just forwards), they would land on the target site and flag us as a scraper. So
        # attach them only when our service is engaged; otherwise the fetch carries no
        # WebClient-identifying headers. Explicit ref/default headers still win.
        using_service = pol is not None and pol.proxy is not None
        headers = {
            **(policy_headers(pol) if using_service else {}),
            **self.default_headers,
            **ref.headers,
        }
        await self._pace(ref.hostname)  # self-guards on the shared engine's interval
        doc, resp = await self._afetch_once(ref, headers)
        attempt = 0
        while doc.error is not None and doc.error.retriable and attempt < max_retries:
            delay = self.retry_backoff * (2**attempt)
            if resp is not None:  # honour a server-sent Retry-After (429/503)
                after = _retry_after_seconds(resp.headers.get("retry-after"))
                if after is not None:
                    delay = min(after, 60.0)  # cap so a huge value can't stall us
            await asyncio.sleep(delay)
            attempt += 1
            doc, resp = await self._afetch_once(ref, headers)
        self._register(doc, ref)
        doc._tiers = ["static"]
        # A protocol-level failure (e.g. ERR_HTTP2_PROTOCOL_ERROR / a reset connection)
        # is a common anti-bot tell -- the server drops a client it dislikes before any
        # response. Under ``auto`` treat it as an anti-bot trigger and escalate to a
        # (stealth, and fingerprinted when configured) browser, whose real TLS/HTTP2
        # stack often clears it. Its own failure then surfaces normally.
        if mode == "auto" and _looks_like_bot_block(doc.error):
            return await self._escalate_to_browser(
                ref, list(doc._events), doc.content,
                tiers=["static", "browser"], keep_alive=keep_alive, wait=wait,
            )
        flags = self._observe(doc, resp)
        # browser="auto": a FLAG-driven escalation ladder. Read the request+static
        # flags; a login wall fails (no transport fixes credentials), an anti-bot
        # challenge escalates to a fresh proxy exit (then a stealth browser for a
        # named vendor), a SPA escalates to a browser render. Re-read after each hop.
        # Bounded: static -> (proxy) -> browser.
        if mode == "auto" and doc.error is None and flags is not None:
            if flags["login_required"].present:  # a credential wall -- fail loudly
                doc.error = WebError(
                    type="LoginRequired",
                    message=_flag_reason(flags["login_required"], "a login wall blocks the content"),
                )
            else:
                tiers = ["static"]
                antibot = flags["anti_bot_triggered"]
                if antibot.present and antibot.remedy in ("proxy", "stealth"):
                    tiers.append("proxy")  # rotate an exit via the proxy service
                    proxy_headers = {**policy_headers(Resolve(proxy=ProxyPolicy.auto())), **headers}
                    doc, resp = await self._afetch_once(ref, proxy_headers)
                    self._register(doc, ref)
                    doc._tiers = list(tiers)
                    flags = self._observe(doc, resp)
                want_browser = flags is not None and doc.error is None and (
                    flags["spa"].present  # render the client-built content
                    # a named-vendor challenge a proxy didn't clear -> a stealth browser
                    or (flags["anti_bot_triggered"].present
                        and flags["anti_bot_triggered"].remedy == "stealth")
                )
                if want_browser:
                    if resp is not None:  # keep the static hop's navigation/network events
                        self._capture(doc, ref, resp)
                    static_doc = doc  # the usable static hop to fall back to
                    try:
                        rendered = await self._escalate_to_browser(
                            ref, list(doc._events), doc.content,
                            tiers=[*tiers, "browser"], keep_alive=keep_alive, wait=wait,
                        )
                    except Exception:  # noqa: BLE001 - a blocked/failed render is not fatal
                        rendered = None
                    if rendered is not None and rendered.ok:
                        return rendered  # the richer, browser-rendered document
                    # The render failed or was blocked (e.g. anti-bot dropped the browser).
                    # ``auto`` is "cheapest that WORKS", so fall back to the ok static hop
                    # rather than lose it -- a partial static list beats no document. If the
                    # static hop also failed, honour ``optional`` on its error.
                    if static_doc.error is None:
                        return static_doc
                    if not optional:
                        raise WebException(static_doc.error, document=static_doc)
                    return static_doc
        if resp is not None:  # emit navigation/network events for the final doc
            self._capture(doc, ref, resp)
        if doc.error is not None and not optional:  # loud by default
            raise WebException(doc.error, document=doc)
        return doc

    def _observe(self, doc: Document, resp: Any) -> "dict[str, Any] | None":
        """The request/static flags of this response (login / anti-bot / SPA), so
        ``afetch`` can decide whether -- and to what tier -- to escalate. Pure detection
        (:mod:`webclient.signals`), so a remote resolve reads the same flags; the
        ``flags`` facet re-derives them (adding rendered/network evidence) on read."""
        if resp is None:
            return None
        chain = [doc.url, doc.final_url] if doc.final_url and doc.final_url != doc.url else [doc.url]
        return flags_from_response(doc.status_code, resp.headers, doc._set_cookies, doc.content, chain)

    async def _escalate_to_browser(
        self,
        ref: Reference,
        static_events: "list[Any] | None" = None,
        static_html: "bytes | None" = None,
        *,
        tiers: "list[str] | None" = None,
        keep_alive: "bool | float" = False,
        wait: "WaitConfig | None" = None,
    ) -> Document:
        """The static tier said this page is JS-gated; render it in a browser. The
        static hop's events are carried onto the browser doc so ``doc.events`` keeps
        the full trail (both tiers); the static HTML is kept so ``skeleton()`` can
        mark server-initial vs client-injected nodes, and the tier trail is recorded
        on the document for the ``transport`` facet."""
        doc = await self._alive(ref, keep_alive=keep_alive, wait=wait)
        doc._static_html = static_html
        doc._tiers = tiers or ["static", "browser"]
        if static_events:
            doc._events = [*static_events, *doc._events]
        await self._arelease(doc)  # content captured; don't hold the page
        return doc

    # -- plan execution (machinery): the surface's sync/async entry ----------
    def execute(self, expr: Any, context: Any = None, *, stream: bool = False) -> Any:
        """Run a recorded plan on this engine (sync bridge). A remote subclass
        swaps this for an HTTP round-trip; ``stream=True`` yields rows."""
        if stream:
            return self._stream(expr, context)
        return _materialize(evaluate(expr, context, client=self))

    async def aexecute(self, expr: Any, context: Any = None) -> Any:
        """Await a plan. An async client runs it loop-natively on the caller's
        loop; a sync client bridges it off its background engine loop (so an async
        caller of a sync client still doesn't block its own loop)."""
        import asyncio

        if self._mode == "async":
            return _materialize(await aevaluate(expr, context, client=self))
        result = await asyncio.wrap_future(
            self.loop().submit(aevaluate(expr, context, client=self))
        )
        return _materialize(result)

    def _stream(self, expr: Any, context: Any) -> Any:
        """Bridge the async row stream to a sync iterator, publishing plan
        events (a ``_pump`` task feeds a bounded queue on the engine loop)."""
        self.bus.publish(PlanEvent(phase="started"))
        count = 0
        for row in self.loop().stream(astream(expr, context, client=self)):
            count += 1
            self.bus.publish(PlanEvent(phase="row"))
            yield row.get() if isinstance(row, Field) else row
        self.bus.publish(PlanEvent(phase="done", detail={"rows": count}))

    async def astream(self, expr: Any, context: Any) -> Any:
        """Async row stream (the same truly-incremental rows as ``_stream``). An
        async client iterates loop-natively on the caller's loop; a sync client
        bridges from its engine loop as rows complete."""
        self.bus.publish(PlanEvent(phase="started"))
        count = 0
        rows = (
            astream(expr, context, client=self)
            if self._mode == "async"
            else self.loop().astream(astream(expr, context, client=self))
        )
        async for row in rows:
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
    ) -> "Session":
        """A new session sharing this engine (a scoped ``Session``)."""
        from ..session import Session

        core = Session(ttl=ttl, session_headers=headers or {}, **kw)
        core.bind(self)
        return core

    # -- crawl ---------------------------------------------------------------
    def crawl(
        self,
        seeds: Any,
        *,
        config: "CrawlConfig | None" = None,
        resume: "CrawlState | None" = None,
        scope: str | None = None,
        auto: bool = True,
        width: int = 10,
        depth: int = 3,
        max_pages: int = 50,
        max_frontier: int = 10000,
        same_origin: bool = True,
        allow_subdomains: bool = True,
        allow_domains: list[str] | None = None,
        deny_domains: list[str] | None = None,
        allow_countries: list[str] | None = None,
        deny_countries: list[str] | None = None,
        include: str | None = None,
        exclude: str | None = None,
        include_xhr: bool = True,
        keywords: list[str] | None = None,
        obey_robots: bool = True,
        browser: "bool | Literal['never', 'auto', 'always']" = "auto",
        resolve: Any = None,
        project: Any = None,
    ) -> "Crawl":
        """A scoped site traversal sharing this engine (a :class:`Crawl` core). Drive it
        with ``crawl.run()`` (batch → read ``.pages``) or ``crawl.step(select)`` (one
        round; ``select`` may be frontier edges/URLs or brand-new URLs to fetch next).

        ``.pages`` is the ``project`` expression evaluated per page. It defaults to
        ``doc.card()`` -- a lean :class:`PageCard` (url / kind / title / description /
        flags / the tier it was fetched at), enough to rebuild a Reference. Pass any
        document expression to reshape retention (``project=wq.doc.markdown()``,
        ``project=wq.doc.extract(...).project()``, or ``project=wq.doc`` to keep whole
        Documents) -- it is serializable, so it runs the same on a remote crawl.

        Tune it with the typed keyword args, or pass a whole :class:`CrawlConfig`
        (``config=`` then wins over the kwargs). ``browser="auto"`` (default) fetches
        static first and renders only pages whose flags say a browser is needed -- the
        best of both worlds; ``resume=`` a prior ``crawl.state()`` continues where it
        stopped. See :class:`CrawlConfig` for scope/country/domain filters, scoring
        weights, and the frontier cap."""
        from ..crawl import Crawl, CrawlConfig, Edge

        if resume is not None:  # continue a prior crawl from its saved state
            crawl = Crawl(
                config=config or resume.config, scope=scope or resume.scope,
                frontier=list(resume.frontier), history=list(resume.history),
            ).bind(self)
            crawl._seen |= set(resume.seen)
            return crawl

        overrides: dict[str, Any] = {} if project is None else {"project": project}
        cfg = config or CrawlConfig(
            max_pages=max_pages, max_depth=depth, width=width, max_frontier=max_frontier,
            same_origin=same_origin, allow_subdomains=allow_subdomains,
            allow_domains=allow_domains or [], deny_domains=deny_domains or [],
            allow_countries=allow_countries or [], deny_countries=deny_countries or [],
            include=include, exclude=exclude, include_xhr=include_xhr,
            keywords=[k.lower() for k in (keywords or [])], obey_robots=obey_robots,
            browser=browser, resolve=resolve,
            order="best-first" if auto else "manual", **overrides,
        )
        urls = _seed_urls(seeds)
        return Crawl(
            config=cfg,
            scope=scope or (from_url(urls[0]).hostname if urls else ""),
            frontier=[Edge(url=u, depth=0) for u in urls],
        ).bind(self)

    # ``sitemap`` (hunt the sitemap.xml) and ``robots`` (hunt the robots.txt) are
    # dispatched IO ops on ``SiteBacking`` -- reached via ``__getattr__``, so remote is
    # a pure dispatch difference. To crawl a sitemap: ``wc.crawl(wc.sitemap(url))``.

    # -- live / browser ------------------------------------------------------
    def inject_script(self, source: str, *, phase: str = "init") -> Self:
        """Install a script on this client's browser pages: ``phase="init"`` runs
        before every navigation (e.g. instrumentation), ``"load"`` once after. On
        top of the scripts the client's backings declare (``Backing.page_scripts``).
        Returns ``self`` for chaining."""
        self._the_engine().inject_script(source, phase)
        return self

    def _browser_scripts(self) -> list[Any]:
        """The page scripts to install on a live page: this client's own
        (``inject_script``) plus the ones its document backings + registered
        backings declare. The backing owns the script; the client installs it."""
        engine = self._the_engine()
        scripts = list(engine._page_scripts)
        for backing in (*Document.BACKINGS, *engine._backings):
            scripts.extend(backing.page_scripts)
        return scripts

    async def _alive(
        self,
        ref: Reference,
        replay: list[dict[str, Any]] | None = None,
        *,
        keep_alive: "bool | float" = False,
        wait: "WaitConfig | None" = None,
    ) -> Document:
        lease = await self.pool.lease("page")
        browser = cast(Any, lease.client)  # the leased BrowserClient (subclass)
        try:
            # the browser client drives the page and hands back the raw facts
            # (``PageResult``); the document's backings turn those into events
            # (``Backing.on_load`` -- ``LiveBacking`` owns the console/network
            # wrapping). The client never reaches into a backing to shape events.
            result = await browser.open(
                ref.dispatch("url"),
                scripts=self._browser_scripts(),
                replay=replay or [],
                wait=wait,
            )
            # the REAL transport facts Playwright reported for the main navigation --
            # true status + response headers, not a fabricated 200/empty (so
            # ``doc.transport()`` and the ``signals`` access facet are accurate on a
            # browser-rendered page). ``status_code`` 0 (no main response) -> keep the
            # old 200 default so ``doc.ok`` still holds for such a render.
            status = result.status_code or 200
            doc = Document(
                url=ref.dispatch("url"),
                final_url=result.final_url,
                kind="html",
                content=result.content,
                status_code=status,
                response_headers=dict(result.headers),
            )
            if not (200 <= status < 300):  # parity with the http path's not-ok doc
                doc.error = error_for(status)
            doc._client = self
            doc._page = browser.page
            doc._lease = lease
            doc._keep_alive = bool(keep_alive)  # caller owns the lifecycle if set
            if isinstance(keep_alive, (int, float)) and not isinstance(keep_alive, bool):
                self._expire_page(doc, float(keep_alive))  # TTL safety-net release
            doc._tiers = ["browser"]  # the tier trail for the transport facet
            self._register(doc, ref)
            # a browser render is a navigation too: emit the NavigationEvent the
            # static path emits (via ``_capture``), so ``doc.events`` is populated
            # for a browser fetch and static/browser parity holds. Navigation first,
            # then the load-time console/network events ``on_load`` appends.
            nav = NavigationEvent(
                request=ref,
                status_code=doc.status_code,
                document_id=doc.id,
                source="core-browser",
            )
            self.bus.publish(nav)
            doc._events.append(nav)
            for backing in doc.choose():  # backings shape the load into events
                backing.on_load(doc, result)
            return doc
        except BaseException:
            await self.pool.release(lease)  # never leak the page lease on failure
            raise

    async def areload(self, core: Document) -> Document:
        ref = core._ref
        if ref is None:
            raise ValueError("cannot reload a document with no source reference")
        if core._page is not None or ref.actions:  # live page / recorded actions
            return await self._alive(ref, replay=list(ref.actions))
        return await self.afetch(ref)  # plain HTTP refetch

    def release(self, doc: Document) -> None:
        """Return a live document's page lease to the pool (sync front door for
        :meth:`_arelease`)."""
        if doc._lease is not None:
            self.loop().run(self._arelease(doc))

    async def _arelease(self, doc: Document) -> None:
        """Release a browser render's page lease from *within* the engine loop (an
        async twin of ``release``): once a content-only path -- probe, auto-escalate,
        a browser crawl -- has captured the rendered HTML, the live page is no longer
        needed, so return it to the pool (freeing the lease) and drop ``_page`` so
        later ``select``/``text_content`` run in-memory on the captured content
        rather than routing to the (loop-bridging) live backing."""
        if doc._lease is not None:
            await self.pool.release(doc._lease)
            doc._lease = None
            doc._page = None

    def _expire_page(self, doc: Document, ttl: float) -> None:
        """Schedule a TTL safety-net release of a kept-alive page: after ``ttl``
        seconds, release it if the caller hasn't already (``_arelease`` is
        idempotent). Runs on the engine loop (``_alive`` is on it), so a forgotten
        keep-alive page can't leak its lease forever."""
        import asyncio

        async def _expire() -> None:
            try:
                await asyncio.sleep(ttl)
                await self._arelease(doc)
            except asyncio.CancelledError:  # client closing -> loop drains us
                pass

        asyncio.ensure_future(_expire())

    # -- naming / recovery ---------------------------------------------------
    def _register(self, doc: Document, ref: Reference) -> None:
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

    def document(self, name: str) -> Document | None:
        """Recover a materialised document by name from any live scope."""
        import time

        for scope in self._scopes():
            obj = scope.get(name)
            if isinstance(obj, Document):
                obj.accessed = time.time()
                return obj
        return None

    def reference(self, name: str) -> Reference | None:
        """Recover a reference by its (root) name from any live scope."""
        for scope in self._scopes():
            obj = scope.get(name)
            if isinstance(obj, Reference):
                return obj
        return None

    def _capture(self, doc: Document, ref: Reference, resp: Any) -> None:
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


_DEFAULT: "WebClient | None" = None
_DEFAULT_LOCK = threading.Lock()


def default_client() -> WebClient:
    """The process-local shared engine, used wherever an operation has no bound
    client -- an unbound reference/plan (``reference(url).resolve()``), a lazy
    root collected without a client, etc. Recreated after it is closed, so every
    such op shares ONE engine (pool + loop) instead of spinning up throwaways.
    Locked, so concurrent first use from several threads constructs exactly one
    engine (an unlocked check-then-set would build -- and leak -- several)."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None or _DEFAULT._closed:
            _DEFAULT = WebClient()
        return _DEFAULT


def async_client(**policy: Any) -> WebClient:
    """A ``WebClient`` in async-dispatcher mode: loop-native (its IO runs on
    the caller's loop, so ``doc = await ac.fetch(url)``). Not a subclass -- the
    mode is an instance flag read by ``bridge``; the async surface is the same
    core typed through the ``Async*`` stubs. Backs ``surfaces.AsyncWebClient``."""
    core = WebClient(**policy)
    core._mode = "async"
    return core


__all__ = [
    "WebClient",
    "async_client",
    "default_client",
    "NameScope",
    "FetchBacking",
    "_materialize",
    "_retry_after_seconds",
]
