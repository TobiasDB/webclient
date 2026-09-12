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
from typing import TYPE_CHECKING, Any, cast, overload

from .facade import _Facade, run_on_core
from .webclient import SearchEngine, WebClientCore
from .models import Reference

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from ..stubs import Lazy, LazyReference

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

    def ref(self, url: str, method: str = "get", **kwargs: Any) -> "LazyReference":
        """A LAZY reference root bound to this client's core: it records ops and
        runs on ``.collect()`` (or ``wc.execute``), on THIS client's core
        (PLAN §8 -- was eager). Build a plain request spec with
        ``Reference.from_url`` if you need to inspect ``.url``/``.path``."""
        from .models import HttpMethod
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
