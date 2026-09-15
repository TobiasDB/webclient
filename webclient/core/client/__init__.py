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
from typing import TYPE_CHECKING, Any, ClassVar, Self, cast

from pydantic import PrivateAttr

from ...clients import BrowserFactory, ClientPool, HTTPXFactory, PageScript
from ...collection import Field
from ...errors import WebError, WebException
from ...events import EventBus
from ...models import NavigationEvent, NetworkEvent, PlanEvent
from ...query.executor import aevaluate, astream, evaluate
from ..document import Document
from ..document.models import ProbeRecord
from ...resiliency import Signals, classify, policy_headers, visible_word_count
from ..reference import Reference, from_url
from ..web_core import Backing, WebCore
from .fetch import FetchBacking
from .loop import EngineLoop
from .models import IWebClient
from .search import SearchBacking
from .sitemap import SitemapBacking

if TYPE_CHECKING:
    from ..crawl import Crawl
    from ..session import Session
    from ...surfaces.lazy import LazyWebClient


_MODES = ("never", "auto", "always", "probe")


def _browser_mode(browser: Any) -> str:
    """Normalise the ``browser`` kwarg to a tier: ``"never"`` (static only),
    ``"auto"`` (static, escalate if JS-gated), ``"always"`` (straight to browser),
    or ``"probe"`` (resolve both tiers and compare -- the explicit diagnostic).
    Accepts a bool, one of those strings, ``AUTO``, or a ``BrowserPolicy`` (its
    ``when``)."""
    if browser is True:
        return "always"
    if not browser:  # False / None
        return "never"
    if isinstance(browser, str):
        return browser if browser in _MODES else "never"
    when = getattr(browser, "when", None)  # a BrowserPolicy
    return when if when in _MODES else "auto"


def _probe_reason(s: Signals) -> str:
    """A short label for the most salient detected signal (ProbeRecord.reason)."""
    if s.anti_bot:
        return s.anti_bot
    if s.blocked:
        return "blocked"
    if s.paywall:
        return "paywall"
    if s.login_wall:
        return "login_wall"
    if s.js_required or s.empty:
        return "js_required"
    return ""


def _seed_urls(seeds: Any) -> list[str]:
    """Normalise crawl seeds -- a URL string, a Reference (surface or core), or a
    list of either -- to a list of URL strings."""
    items = seeds if isinstance(seeds, (list, tuple)) else [seeds]
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


class WebClient(WebCore, IWebClient):
    """The engine: its Core Fields (policy) + eager verbs come from the
    ``IWebClient`` model/interface it inherits (:mod:`.models`); this core adds the
    machinery (loop, ClientPool, bus, name scopes, transport + plan execution). Its
    user-facing verbs are backings (``FetchBacking`` / ``SearchBacking``); a remote
    backend is just a subclass that swaps ``execute``, sessions a scoped subclass."""

    if TYPE_CHECKING:  # narrow WebCore.lazy (Any) to this core's lazy surface

        @property
        def lazy(self) -> "LazyWebClient": ...

    _loop: Any = PrivateAttr(default=None)
    _pool: Any = PrivateAttr(default=None)  # ClientPool (lazy)
    #: backings registered via ``use(...)``, chosen before the built-ins (newest
    #: first) by every core bound to this client -- the extensibility hook.
    _backings: list[Backing] = PrivateAttr(default_factory=list)
    #: browser page scripts injected directly on this client (``inject_script``),
    #: on top of the ones its backings declare (see ``_browser_scripts``).
    _page_scripts: list[Any] = PrivateAttr(default_factory=list)
    _closed: bool = PrivateAttr(default=False)
    _scope: Any = PrivateAttr(default=None)  # the client's NameScope (000)
    _scope_counter: int = PrivateAttr(default=0)  # next session scope index
    _scope_lock: Any = PrivateAttr(default_factory=threading.Lock)  # guards ^
    _bus: Any = PrivateAttr(default=None)  # EventBus (lazy)
    _sessions: list[Any] = PrivateAttr(default_factory=list)  # sessions to close
    _host_next: dict[str, float] = PrivateAttr(  # host -> earliest next request time
        default_factory=dict
    )
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
        self._pool = ClientPool(
            {"http": HTTPXFactory(), "page": BrowserFactory()},
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

    BACKINGS: ClassVar[tuple[Backing, ...]] = (
        FetchBacking(),
        SearchBacking(),
        SitemapBacking(),
    )

    # -- loop / lifecycle ----------------------------------------------------
    def loop(self) -> EngineLoop:
        if self._loop is None:
            self._loop = EngineLoop()
        return cast(EngineLoop, self._loop)

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
        return cast(ClientPool, self._pool)

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
            if self._pool is not None:
                await self._pool.aclose()
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
        ``host``. The schedule lives on the shared ENGINE (a session paces against
        its parent), so N sessions on one engine honour ONE per-host rate limit
        rather than each keeping an independent schedule. Best-effort; concurrent
        same-host fetches may still bunch."""
        import asyncio
        import time

        engine: WebClient = getattr(self, "_parent", None) or self
        interval = engine.min_interval
        if interval <= 0.0:
            return
        schedule = engine._host_next
        wait = schedule.get(host, 0.0) - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        now = time.monotonic()
        schedule[host] = now + interval
        if len(schedule) > 4096:  # bound the map: drop hosts whose window has passed
            engine._host_next = {h: t for h, t in schedule.items() if t > now}

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
        self, ref: Reference, *, optional: bool = False, browser: Any = False
    ) -> Document:
        """Resolve ``ref`` into a document over a leased transport (http) or a
        browser page. ``browser`` picks the tier: ``False``/``"never"`` = static
        only, ``True``/``"always"`` = straight to a browser, ``"auto"`` (or a
        ``BrowserPolicy(when="auto")``) = static first, escalating to a browser
        render only when the page is JS-gated (the Crawlee adaptive rule). A
        retriable failure is retried up to ``retries`` times with exp backoff."""
        import asyncio

        mode = _browser_mode(browser)
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
            return await self._alive(ref)
        if mode == "probe":
            return await self._probe_compare(ref)
        # declare the resolve policy (rate/retry/proxy) to a downstream proxy
        # service as X-WebClient-* headers; explicit headers still win over them.
        headers = {
            **policy_headers(self.resolve),
            **self.default_headers,
            **ref.headers,
        }
        await self._pace(ref.hostname)  # self-guards on the shared engine's interval
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
        signals = self._observe(doc, resp)  # record what would escalate (P1)
        # P2: browser="auto" -- escalate a JS-gated static page to a browser render.
        if (
            mode == "auto"
            and doc.error is None
            and signals is not None
            and signals.needs_browser
        ):
            if resp is not None:  # keep the static hop's navigation/network events
                self._capture(doc, ref, resp)
            return await self._escalate_to_browser(
                ref, signals, list(doc._events), doc.content
            )
        if resp is not None:  # emit navigation/network events for the final doc
            self._capture(doc, ref, resp)
        if doc.error is not None and not optional:  # loud by default
            raise WebException(doc.error, document=doc)
        return doc

    def _observe(self, doc: Document, resp: Any) -> "Signals | None":
        """Resiliency P1 (observe): classify the static response and, if anything
        notable is detected (anti-bot / JS-gated / paywall / login wall / hard
        block), record it onto the document for the ``probe`` summary facet. Returns
        the signals so ``afetch`` can decide whether to escalate. Pure detection
        (:mod:`webclient.resiliency.detect`), so a remote resolve records the same."""
        if resp is None:
            return None
        signals = classify(doc.status_code, resp.headers, doc._set_cookies, doc.content)
        if not signals.any:
            return signals
        doc._probe = ProbeRecord(
            was_browser_required=False,
            anti_bot=signals.anti_bot,
            js_required=signals.js_required,
            paywall=signals.paywall,
            login_wall=signals.login_wall,
            escalation=["static"],
            reason=_probe_reason(signals),
            final_tier="static",
        )
        return signals

    async def _escalate_to_browser(
        self,
        ref: Reference,
        signals: "Signals",
        static_events: "list[Any] | None" = None,
        static_html: "bytes | None" = None,
    ) -> Document:
        """The static tier said this page is JS-gated; render it in a browser and
        record the two-tier trail on the resulting document (the ``probe`` facet).
        The static hop's events are carried onto the browser doc so ``doc.events``
        keeps the full trail (both tiers); the static HTML is kept so ``skeleton()``
        can mark server-initial vs client-injected nodes."""
        doc = await self._alive(ref)
        doc._static_html = static_html
        if static_events:
            doc._events = [*static_events, *doc._events]
        doc._probe = ProbeRecord(
            was_browser_required=True,
            js_required=True,
            anti_bot=signals.anti_bot,
            escalation=["static", "browser"],
            reason="js_required",
            attempts=2,
            final_tier="browser",
        )
        await self._arelease(doc)  # content captured; don't hold the page
        return doc

    async def _probe_compare(self, ref: Reference) -> Document:
        """``browser="probe"``: resolve *both* tiers and compare, then return the
        fuller (browser) document carrying an accurate ``probe`` facet -- the
        explicit "can I scrape this / what do I need" diagnostic. It measures how
        much visible content the browser render recovers over the static response
        (``render_gain``) and reports ``was_browser_required`` definitively (rather
        than the conservative ``auto`` heuristic), so a full, content-complete
        summary can be built with an accurate account of what the page needed."""
        static = await self.afetch(ref, browser=False, optional=True)
        try:
            browser = await self._alive(ref)
        except Exception:  # browser tier unavailable -> the static doc is all we have
            if static._probe is not None:
                static._probe.reason = "browser_unavailable"
            return static
        browser._static_html = static.content  # for skeleton() origin annotation
        static_words = visible_word_count(static.content) if static.ok else 0
        browser_words = visible_word_count(browser.content)
        gain = max(0, browser_words - static_words)
        # content the browser actually recovered: a real gain in visible words
        # (>= 20) that is either everything (static was empty) or a clear >=25%
        # growth. A sparse static page the browser does NOT enrich (gain == 0) is
        # NOT flagged -- render_gain == 0 must mean "static already carried it".
        content_gated = static.ok and gain >= 20 and (
            static_words == 0 or browser_words >= static_words * 1.25
        )
        sp = static._probe  # what the static tier detected (anti-bot / js / walls)
        required = bool(content_gated or not static.ok)
        browser._probe = ProbeRecord(
            was_browser_required=required,
            js_required=bool(content_gated),
            anti_bot=sp.anti_bot if sp else None,
            paywall=sp.paywall if sp else False,
            login_wall=sp.login_wall if sp else False,
            render_gain=gain,
            escalation=["static", "browser"],
            reason=(
                "js_injected_content" if content_gated
                else "static_blocked" if not static.ok
                else "static_sufficient"
            ),
            attempts=2,
            final_tier="browser",
        )
        await self._arelease(browser)  # diagnostic done; release the compared page
        return browser

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
        scope: str | None = None,
        auto: bool = False,
        width: int = 10,
        depth: int = 3,
        max_pages: int = 50,
        same_origin: bool = True,
        obey_robots: bool = True,
        browser: bool = False,
        keywords: list[str] | None = None,
        include: str | None = None,
        exclude: str | None = None,
        facets: list[str] | None = None,
    ) -> "Crawl":
        """A scoped site traversal sharing this engine (a :class:`Crawl` core). The
        client manages the frontier (dedup, scope, fetching); the caller steers each
        round (``crawl.step(select)``) or lets it self-drive (``auto=True`` -> the
        top-``width`` edges best-first by ``keywords``). Use as a context manager.
        ``facets`` picks which summary backings each fetched page carries (``None``
        / empty -> the lean ``crawl.DEFAULT_FACETS``, not every facet -- a full
        summary per page is wasteful at crawl scale; pass ``facets=list(FACETS)``
        for the full summary)."""
        from ..crawl import Crawl, Edge
        from ..crawl.models import DEFAULT_FACETS

        urls = _seed_urls(seeds)
        core = Crawl(
            scope=scope or (from_url(urls[0]).hostname if urls else ""),
            auto=auto,
            width=width,
            max_depth=depth,
            max_pages=max_pages,
            same_origin=same_origin,
            obey_robots=obey_robots,
            browser=browser,
            keywords=[k.lower() for k in (keywords or [])],
            include=include,
            exclude=exclude,
            facets=list(facets or DEFAULT_FACETS),
            frontier=[Edge(url=u, depth=0) for u in urls],
        )
        return core.bind(self)

    def sitemap(
        self,
        url: Any,
        *,
        depth: int = 2,
        width: int = 20,
        max_pages: int = 1000,
        use_sitemap_xml: bool = True,
    ) -> "Crawl":
        """Map a site: an eager, single-domain :meth:`crawl` in auto mode, run to
        completion -- HEAVY (fetches up to ``max_pages`` pages). Returns the finished
        crawl -- a ``.summary()`` per page in ``.pages`` plus the unresolved
        ``.frontier`` edges. (For just the list of sitemap URLs, use the cheap
        :meth:`discover_sitemaps` instead -- ``sitemap`` runs a crawl.) ``use_sitemap_xml``
        (default on) first discovers the site's real ``sitemap.xml`` URLs
        (:meth:`discover_sitemaps`) and seeds the frontier with them, so a declared sitemap
        is honoured; it still link-crawls to fill in whatever the sitemap omits."""
        seeds: list[Any] = [url]
        if use_sitemap_xml:
            discovered = self.dispatch("discover_sitemaps", url)
            seeds += [r.url for r in discovered]
        return self.crawl(
            seeds, auto=True, depth=depth, width=width, max_pages=max_pages
        ).run()

    # -- live / browser ------------------------------------------------------
    def inject_script(self, source: str, *, phase: str = "init") -> Self:
        """Install a script on this client's browser pages: ``phase="init"`` runs
        before every navigation (e.g. instrumentation), ``"load"`` once after. On
        top of the scripts the client's backings declare (``Backing.page_scripts``).
        Returns ``self`` for chaining."""
        self._page_scripts.append(PageScript(source, cast(Any, phase)))
        return self

    def _browser_scripts(self) -> list[Any]:
        """The page scripts to install on a live page: this client's own
        (``inject_script``) plus the ones its document backings + registered
        backings declare. The backing owns the script; the client installs it."""
        scripts = list(self._page_scripts)
        for backing in (*Document.BACKINGS, *self._backings):
            scripts.extend(backing.page_scripts)
        return scripts

    async def _alive(
        self, ref: Reference, replay: list[dict[str, Any]] | None = None
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
            )
            doc = Document(
                url=ref.dispatch("url"),
                final_url=result.final_url,
                kind="html",
                content=result.content,
                status_code=200,
            )
            doc._client = self
            doc._page = browser.page
            doc._lease = lease
            # the read-side of the resiliency ladder: this document needed a real
            # browser (P0 records the fact; later phases fill the rest of the trail).
            doc._probe = ProbeRecord(
                was_browser_required=True,
                final_tier="browser",
                escalation=["browser"],
            )
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
        """Return a live document's page lease to the pool."""
        if doc._lease is not None:
            self.loop().run(self.pool.release(doc._lease))
            doc._lease = None
            doc._page = None

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
    "SearchBacking",
    "_materialize",
    "_retry_after_seconds",
]
