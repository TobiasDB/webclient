"""The public clients over a core backend.

``WebClient`` (sync) and ``AsyncWebClient`` (async) wrap *any* core that
exposes the common interface (``resolve``/``execute``/``astream``/``aclose``/
``_ensure_loop``/``session``) and share every plan-building helper from
:class:`webclient.client._Facade`. They differ only in how ``_run`` drives
the core: ``WebClient`` bridges onto the core's engine loop and blocks,
``AsyncWebClient`` awaits it on the caller's loop. The backend is pluggable:
a local :class:`WebClientCore` runs plans in-process, and
``RemoteWebClientCore`` (remote.py) runs them on a service over HTTP --
``WebClient(core=RemoteWebClientCore(url, token))``.
"""
from __future__ import annotations

import atexit
import threading
from typing import Any

from .client import _Facade, run_on_core
from .core.webclient import SearchEngine, WebClientCore
from .document import Reference

__all__ = ["WebClient", "AsyncWebClient", "SearchEngine", "WebClientCore",
           "default_client"]


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

    def ref(self, url: str, method: str = "get", **kwargs: Any) -> Reference:
        """A Reference bound to this client's core."""
        return Reference.from_url(url, method=method, **kwargs).bind(self._core)

    def _bind(self, ref: Any, session: Any = None) -> Any:
        """Bind a bare Reference to this core so ``resolve`` runs eagerly."""
        if isinstance(ref, Reference) and ref._client is None:
            return ref.bind(self._core, session or ref._session)
        return ref

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
