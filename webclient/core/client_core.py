"""WebClientCore: the engine core (rewrite skeleton).

The lifecycle root: owns the ClientPool (fronted by ClientFactories), the event
bus, plugins, name scopes and the engine loop. Its backings are the verbs the
client offers -- Fetch, Search, Crawl, Session, Document. Everything async; the
sync surface bridges onto the loop. Remote is the same core with ``execute``
swapped for a POST / websocket round-trip (eager parity).
"""
from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, PrivateAttr

from .web_core import Backing, WebCore


class ClientFactory:
    """Builds + configures one Client (http / browser / remote) for the pool.
    Replaces the scattered ad-hoc client construction -- one place that knows
    how to make each kind, so ``ClientPool`` just asks a factory."""

    kind: str = ""

    def create(self, **config: Any) -> Any: ...          # TODO
    async def aclose(self, client: Any) -> None: ...      # TODO


class WebClientCore(WebCore, BaseModel):
    """Core Fields (policy) + owned infrastructure + backings. The user-facing
    ``WebClient`` / ``AsyncWebClient`` are the generated surface; this is the
    machinery they dispatch into."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # -- Core Fields (policy) ------------------------------------------------
    timeout: float = 30.0
    headless: bool = True
    # TODO(port): retries / events_cap / verify_tls / default_headers / proxies.

    # -- owned infrastructure (PrivateAttr: runtime state) -------------------
    _loop: Any = PrivateAttr(default=None)       # EngineLoop (reuse engine/loop.py)
    _pool: Any = PrivateAttr(default=None)       # ClientPool(factories=...)
    _bus: Any = PrivateAttr(default=None)
    _sessions: dict[str, Any] = PrivateAttr(default_factory=dict)
    _closed: bool = PrivateAttr(default=False)

    # -- backings: the client's verbs ---------------------------------------
    BACKINGS: ClassVar[tuple[Backing, ...]] = ()   # TODO: (Fetch, Search, Crawl, Session, Document)

    # -- execution (the whole core is async; sync bridges onto the loop) -----
    def execute(self, expr: Any, context: Any = None) -> Any:
        """Sync: run ``aexecute`` on the engine loop and block."""
        return self._loop.run(self.aexecute(expr, context))   # TODO(port): EngineLoop

    async def aexecute(self, expr: Any, context: Any = None) -> Any:
        """Async: evaluate a lazy plan (reuse the current executor walk)."""
        ...   # TODO(port): executor.evaluate(expr, context, client=self)

    def remote_execute(self, expr: Any, context: Any = None) -> Any:
        """Sync: run ``aremote_execute`` on the loop."""
        return self._loop.run(self.aremote_execute(expr, context))

    async def aremote_execute(self, expr: Any, context: Any = None) -> Any:
        """Async: POST / websocket ``/execute`` to the service (eager parity).
        The ONLY difference from a local core -- same plan, remote evaluation."""
        ...   # TODO: websocket/POST round-trip


__all__ = ["WebClientCore", "ClientFactory"]
