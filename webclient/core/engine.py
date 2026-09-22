"""Engine: the shared transport/execution resources a ``WebCore`` is bound to.

The Engine owns the event loop, the transport (a local ``ClientPool`` of leased http
clients + browser pages, OR -- in ``"remote"`` mode -- a :class:`~.service.ServiceTransport`
that POSTs plans to a service), the event bus, the dispatch mode, per-host pacing, injected
page scripts, and the backings registered via ``use()`` -- the resources a ``WebClient`` and
every session scoped on it SHARE. A session never owns an engine; it borrows its parent's
(see :meth:`WebClient._the_engine`), so the whole session tree shares one loop / pool /
bus / mode.
"""

from __future__ import annotations

import logging
from collections import deque
from pathlib import Path
from typing import Any, cast

from ..clients import BrowserFactory, ClientPool, HTTPXFactory
from ..events import EventBus

log = logging.getLogger(__name__)


class Engine:
    """The shared resources one ``WebClient`` owns and its sessions borrow: the engine
    loop, the transport pool, the event bus, per-host pacing state, and page scripts."""

    def __init__(self, browser_config: Any, *, transport: bool = True, har: "str | None" = None) -> None:
        #: the dispatch mode -- how/where ops on THIS engine execute: ``"sync"`` blocks
        #: IO on the background loop, ``"async"`` is loop-native, ``"remote"`` turns every
        #: op into an API call. A property of the engine, so every session scoped on it
        #: shares one mode (``remote`` is just a dispatch of the engine, not a per-core flag).
        self._mode: str = "sync"
        self._loop: Any = None  # EngineLoop (lazy)
        self._pool: Any = None  # ClientPool (None when transport=False, e.g. a remote client)
        #: the REMOTE transport when ``_mode == "remote"`` (a ``core.service.ServiceTransport``),
        #: else None. An engine in remote mode holds this instead of a local pool; every op's
        #: plan is POSTed through it (see :meth:`execute`). Set by ``WebClient.remote``.
        self._service: Any = None
        self._bus: Any = None  # EventBus (lazy)
        self._errors: "deque[Any]" = deque()  # the error ledger (sized with the bus)
        self._host_next: dict[str, float] = {}  # host -> earliest next request time
        #: the named script table (roadmap N8): the backings' declared scripts, the client's
        #: ``inject_script``s and any registered by hand, plus the policy. Built lazily.
        self._scripts: Any = None
        #: backings registered via ``use(...)`` -- chosen (newest first) before the
        #: built-in BACKINGS by every core bound to this engine. Shared across the
        #: sessions scoped on it, so a ``use()`` on the client or any session is seen
        #: by all of them (the extensibility hook).
        self._backings: list[Any] = []
        #: the active trace directory while ``WebClient.trace()`` is open (None otherwise):
        #: the client emits snapshots + captures bodies/headers only then, and the browser
        #: factory records a HAR per context into ``<trace>/har``.
        self.trace_dir: "str | None" = None
        self._har = har
        if transport:  # a remote client executes over the wire -- no local pool, but keep bus/loop
            self._init_transport(browser_config)

    def _init_transport(self, bc: Any) -> None:
        """Build the transport pool eagerly (cheap -- no browser launch until a page is
        leased) so it is never lazily created from two threads at once."""
        from ..settings import current

        self._pool = ClientPool(
            {
                "http": HTTPXFactory(proxy=bc.proxy, har=self._har),  # same client-wide proxy for httpx...
                "page": BrowserFactory(
                    headless=bc.headless, stealth=bc.stealth, fingerprint=bc.fingerprint,
                    channel=bc.channel, proxy=bc.proxy,  # ...and the browser
                    cdp_endpoint=bc.cdp_endpoint, ws_endpoint=bc.ws_endpoint,
                    reuse_context=bc.reuse_context, replay_har=bc.replay_har or self._har,
                ),
            },
            limits={"http": bc.pool_http, "page": bc.pool_pages},
            acquire_timeout=current().limits.pool_acquire_timeout,
        )
        log.debug("engine transport: http=%d pages=%d headless=%s stealth=%s",
                  bc.pool_http, bc.pool_pages, bc.headless, bc.stealth)

    # -- remote dispatch: the engine's transport IS the difference ------------
    @property
    def is_remote(self) -> bool:
        """Whether this engine dispatches over a remote :mod:`.service` transport."""
        return self._service is not None

    def go_remote(self, url: str, token: str | None, timeout: float) -> None:
        """Put this engine into ``"remote"`` mode over the service at ``url`` -- swap the
        local pool for a :class:`~.service.ServiceTransport` the engine dispatches through."""
        from .service import ServiceTransport

        self._mode = "remote"
        self._pool = None  # no local pool: execution is a remote round-trip
        self._service = ServiceTransport(url, token, timeout)
        log.info("engine in remote mode -> %s", url)

    def execute(self, client: Any, expr: Any, context: Any = None, *, stream: bool = False) -> Any:
        """POST a plan over the remote transport (remote mode only). The engine decides how
        the result comes back -- a lazy handle for a document, real data for a scalar."""
        return self._service.execute(client, expr, context, stream=stream)

    def open_server_session(self, ttl: float | None) -> str:
        """Open a server-side session over the remote transport; returns its id."""
        return cast(str, self._service.open_session(ttl))

    def close_server_session(self, sid: str) -> None:
        """Dispose the server-side session ``sid`` over the remote transport."""
        self._service.close_session(sid)

    def loop(self) -> Any:
        """The engine loop -- one background asyncio ``EngineLoop`` the sync client bridges
        blocking IO onto (created lazily, shared by every session on this engine)."""
        if self._loop is None:
            from .client.loop import EngineLoop  # lazy: avoid a client<->engine import cycle

            self._loop = EngineLoop()
        return self._loop

    @property
    def tracing(self) -> bool:
        """Whether a trace is being written on this engine (snapshots / bodies are captured)."""
        return self.trace_dir is not None

    def start_trace(self, path: str) -> None:
        """Mark a trace as active: the browser factory records a HAR per context under it."""
        self.trace_dir = path
        if self._pool is not None:
            factory = self._pool._factories.get("page")
            if factory is not None:
                factory.har_dir = str(Path(path) / "har")

    def stop_trace(self) -> None:
        self.trace_dir = None
        if self._pool is not None:
            factory = self._pool._factories.get("page")
            if factory is not None:
                factory.har_dir = None

    @property
    def pool(self) -> ClientPool:
        """The transport lease pool (http clients + browser pages); ``None`` on a remote
        engine, which dispatches over its service transport instead of a local pool."""
        return self._pool  # type: ignore[no-any-return]

    @property
    def bus(self) -> EventBus:
        """The shared event bus every op/observation publishes to (created lazily, one per
        engine, so all sessions on it see one stream)."""
        if self._bus is None:
            from ..settings import current

            limits = current().limits
            self._bus = EventBus(history=limits.event_history)
            self._errors = deque(maxlen=max(0, limits.error_ledger))
            self._bus.subscribe("error", self._errors.append)  # the engine's error ledger
        return self._bus  # type: ignore[no-any-return]

    @property
    def errors(self) -> "list[Any]":
        """The engine's ERROR LEDGER: every :class:`~webclient.models.ErrorEvent` published on
        this engine (raised, returned or swallowed), oldest first, bounded by
        ``limits.error_ledger``. Nothing that went wrong on this engine is missing from it."""
        self.bus  # ensure the ledger subscription exists
        return list(self._errors)

    @property
    def scripts(self) -> Any:
        """The engine's :class:`~webclient.scripts.ScriptRegistry` (created lazily; the
        document backings' declared scripts are registered on first use)."""
        if self._scripts is None:
            from ..scripts import ScriptRegistry

            self._scripts = ScriptRegistry(bus=self.bus)
        return self._scripts

    def inject_script(self, source: str, phase: str = "init") -> None:
        """Register a user page script (``phase="init"`` before every nav / ``"load"`` after)."""
        self.scripts.inject(source, phase)

    async def pace(self, host: str, min_interval: float) -> None:
        """Politeness: keep at least ``min_interval`` seconds between requests to ``host``.
        The schedule lives HERE (on the shared engine), so N sessions on one engine honour
        ONE per-host rate limit rather than each keeping an independent schedule."""
        import asyncio
        import time

        if min_interval <= 0.0:
            return
        schedule = self._host_next
        wait = schedule.get(host, 0.0) - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        now = time.monotonic()
        schedule[host] = now + min_interval
        from ..settings import current

        if len(schedule) > current().limits.host_schedule_max:  # bound the map: drop passed windows
            self._host_next = {h: t for h, t in schedule.items() if t > now}

    def close(self) -> None:
        """Tear down the transport: the remote service connection, or the local pool on the
        engine loop, then stop the loop."""
        log.debug("engine close (mode=%s)", self._mode)
        if self._service is not None:
            self._service.close()
        if self._loop is not None and not self._loop.closed and self._pool is not None:
            self._loop.run(self._pool.aclose())
        if self._loop is not None:
            self._loop.stop()

    async def aclose_async(self) -> None:
        """Close the pool loop-natively (the async client's teardown, on the caller's loop)."""
        if self._pool is not None:
            await self._pool.aclose()


__all__ = ["Engine"]
