"""Engine: the shared transport/execution resources a ``WebCore`` is bound to.

The Engine owns the event loop, the transport ``ClientPool`` (leased http clients +
browser pages), the event bus, per-host pacing, and injected page scripts -- the
resources a ``WebClient`` and every session scoped on it SHARE. A session never owns
an engine; it borrows its parent's (see :meth:`WebClient._the_engine`).

Step 1a of the session-base refactor: this is extracted from ``WebClient`` with NO
behaviour change. The name-scope allocator and the session registry still live on the
client for now (they move onto the session base in a later sub-step); the Engine is
purely the transport/loop/bus/pacing resource holder that the client delegates to.
"""

from __future__ import annotations

from typing import Any

from ..clients import BrowserFactory, ClientPool, HTTPXFactory, PageScript
from ..events import EventBus


class Engine:
    """The shared resources one ``WebClient`` owns and its sessions borrow: the engine
    loop, the transport pool, the event bus, per-host pacing state, and page scripts."""

    def __init__(self, browser_config: Any, *, transport: bool = True) -> None:
        self._loop: Any = None  # EngineLoop (lazy)
        self._pool: Any = None  # ClientPool (None when transport=False, e.g. a remote client)
        self._bus: Any = None  # EventBus (lazy)
        self._host_next: dict[str, float] = {}  # host -> earliest next request time
        self._page_scripts: list[Any] = []  # scripts injected via inject_script
        if transport:  # a remote client executes over the wire -- no local pool, but keep bus/loop
            self._init_transport(browser_config)

    def _init_transport(self, bc: Any) -> None:
        """Build the transport pool eagerly (cheap -- no browser launch until a page is
        leased) so it is never lazily created from two threads at once."""
        self._pool = ClientPool(
            {
                "http": HTTPXFactory(proxy=bc.proxy),  # same client-wide proxy for httpx...
                "page": BrowserFactory(
                    headless=bc.headless, stealth=bc.stealth, fingerprint=bc.fingerprint,
                    channel=bc.channel, proxy=bc.proxy,  # ...and the browser
                ),
            },
            limits={"http": bc.pool_http, "page": bc.pool_pages},
        )

    def loop(self) -> Any:
        if self._loop is None:
            from .client.loop import EngineLoop  # lazy: avoid a client<->engine import cycle

            self._loop = EngineLoop()
        return self._loop

    @property
    def pool(self) -> ClientPool:
        return self._pool  # type: ignore[no-any-return]

    @property
    def bus(self) -> EventBus:
        if self._bus is None:
            self._bus = EventBus()
        return self._bus  # type: ignore[no-any-return]

    def inject_script(self, source: str, phase: str = "init") -> None:
        """Register a page script (``phase="init"`` before every nav / ``"load"`` after)."""
        self._page_scripts.append(PageScript(source, phase))  # type: ignore[arg-type]

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
        if len(schedule) > 4096:  # bound the map: drop hosts whose window has passed
            self._host_next = {h: t for h, t in schedule.items() if t > now}

    def close(self) -> None:
        """Tear down the transport (close the pool on the engine loop, then stop it)."""
        if self._loop is not None and not self._loop.closed and self._pool is not None:
            self._loop.run(self._pool.aclose())
        if self._loop is not None:
            self._loop.stop()

    async def aclose_async(self) -> None:
        """Close the pool loop-natively (the async client's teardown, on the caller's loop)."""
        if self._pool is not None:
            await self._pool.aclose()


__all__ = ["Engine"]
