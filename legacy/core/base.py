"""Error policy + capability, as one decorator (spec.py: "implemented as a
decorator to avoid bloating the code base").

Recording lives entirely in ``Expr`` now; a model method is a plain method
that does the real work and raises on failure. ``@policy`` wraps it so that
eager callers get the ``error=IGNORE|RETURN|RAISE`` envelope and capability
checks, and the evaluator -- which replays a plan by calling these same
methods -- gets the mode-appropriate default (RAISE eager, RETURN in a
plan). There is no registry and no whitelist: the evaluator dispatches by
``getattr`` and the only boundary is that ``Expr`` never records a
``_``-prefixed name.
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import json as _json
from collections import OrderedDict
from collections.abc import Iterator
from contextvars import ContextVar
from typing import (TYPE_CHECKING, Any, Callable, ClassVar, Literal, TypeVar,
                    cast, overload)

from pydantic import BaseModel, PrivateAttr
from typing_extensions import Self

if TYPE_CHECKING:
    from .document import Document, Reference

F = TypeVar("F", bound=Callable[..., Any])

ErrorPolicy = Literal["error-ignore", "error-return", "error-raise"]
IGNORE: ErrorPolicy = "error-ignore"   # ok=True, error kept for diagnostics
RETURN: ErrorPolicy = "error-return"   # ok=False, error set, object returned
RAISE: ErrorPolicy = "error-raise"     # raise where you stand

Capability = Literal["tree", "page", "ok"]

#: the default when a call carries no explicit ``error=``. Eager = RAISE;
#: the evaluator sets RETURN around a plan so one bad row cannot abort it.
_DEFAULT: ContextVar[ErrorPolicy] = ContextVar("default_policy", default=RAISE)
_PREFIX = {"Reference": "ref", "Document": "doc", "Collection": "col"}

#: model classes by name, for building a not-ok result of the right type on
#: failure. Populated at the end of models.py -- not a whitelist.
CLASSES: dict[str, Any] = {}


class OpError(Exception):
    """The carried ``WebError`` of a not-ok object, raised under RAISE."""

    def __init__(self, error: Any) -> None:
        super().__init__(f"{error.type}: {error.message}" if error else "not ok")
        self.error = error


class UnsupportedOperation(TypeError):
    """A method needed a capability the receiver's backing cannot serve."""

    def __init__(self, name: str, require: str, have: frozenset[str]) -> None:
        super().__init__(
            f"{name}() requires {require!r}; this object has "
            f"{sorted(have) or 'no capabilities'} (resolve it, or re-resolve "
            "with browser=True for 'page')")


class EngineCore:
    """Shared lifecycle for a core backend: a lazily-created engine loop and a
    synchronous ``close`` that bridges an async ``aclose``. Subclasses supply
    the ``_loop``/``_loop_lock``/``_closed`` state (a pydantic core via
    ``PrivateAttr``, a plain core in ``__init__``) and implement ``aclose``.
    The loop exists only to serve a *sync* caller; an async caller awaits the
    core's coroutines on its own loop and never creates one."""

    def _ensure_loop(self) -> Any:
        if self._closed:
            raise RuntimeError(f"{type(self).__name__} is closed")
        with self._loop_lock:
            if self._loop is None:
                from ..engine.loop import EngineLoop
                self._loop = EngineLoop()
        return self._loop

    async def aclose(self) -> None:
        raise NotImplementedError

    def _finalize(self) -> None:
        """Run after ``close`` marks the core closed (e.g. mark sessions)."""

    def close(self) -> None:
        if self._closed:
            return
        if self._loop is not None and not self._loop.closed:
            self._loop.run(self.aclose())
        self._closed = True
        self._finalize()
        if self._loop is not None:
            self._loop.stop()

    def __enter__(self) -> "EngineCore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class NameScope:
    """Names are scoped to their resolver and retained, strongly, while it
    lives (Decision 9): the client scope is index 000 and LRU-capped; each
    session scope is dropped whole when the session closes."""

    def __init__(self, index: int, cap: int | None = None) -> None:
        self.index = index
        self.cap = cap
        self._seq = 0
        self._objects: OrderedDict[str, WebBase] = OrderedDict()

    def reserve(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}:{self.index:03d}-{self._seq:03d}"

    def add(self, obj: WebBase) -> str:
        if not obj.name:
            prefix = next((_PREFIX[k.__name__] for k in type(obj).__mro__
                           if k.__name__ in _PREFIX), "obj")
            obj.name = self.reserve(prefix)
        stamp = now()
        obj.created = obj.created or stamp
        obj.updated = obj.accessed = stamp
        self._objects[obj.name] = obj
        if self.cap is not None:
            while len(self._objects) > self.cap:
                self._objects.popitem(last=False)
        return obj.name

    def get(self, name: str) -> WebBase | None:
        obj = self._objects.get(name)
        if obj is not None:
            self._objects.move_to_end(name)
            obj.accessed = now()
        return obj

    def clear(self) -> None:
        self._objects.clear()

    def __len__(self) -> int:
        return len(self._objects)



def default_policy(pol: ErrorPolicy) -> Any:
    """Context manager: set the default error policy for the enclosed calls
    (the evaluator wraps a plan in ``default_policy(RETURN)``)."""
    token = _DEFAULT.set(pol)

    class _Ctx:
        def __enter__(self) -> None: ...
        def __exit__(self, *exc: object) -> None:
            _DEFAULT.reset(token)
    return _Ctx()


def now() -> int:
    import time
    return int(time.time() * 1000)


def policy(*, returns: str = "Self", require: Capability | None = None,
           always: bool = False) -> Callable[[F], F]:
    """Wrap a WebBase method with the error/capability envelope.

    ``returns`` names the class of a not-ok result built on failure (``Self``
    = the receiver; ``"None"`` = a non-WebBase value, so IGNORE gives None
    and RETURN the WebError). ``require`` is a live-backing capability
    checked every call. ``always`` runs even on a not-ok receiver."""
    def decorate(fn: F) -> F:
        @functools.wraps(fn)
        def invoke(self: Any, *args: Any, **kwargs: Any) -> Any:
            pol: ErrorPolicy = kwargs.pop("error", None) or _DEFAULT.get()
            self.accessed = now()
            if require is not None:
                # the only ``require`` is 'ok' (resolve); backing capabilities
                # (tree/page) are enforced by DocumentCore.backing, not here.
                have = frozenset({"ok"}) if self.ok else frozenset()
                if require not in have:
                    exc = UnsupportedOperation(fn.__name__, require, have)
                    return _fail(self, returns, exc, pol)
            if not self.ok and not always:
                if pol == RAISE:
                    raise _tag(OpError(self.error))
                return _carry(self, returns, self.error, self.message, ok=False)
            try:
                result = fn(self, *args, **kwargs)
            except Exception as exc:
                return _fail(self, returns, exc, pol)
            if inspect.isawaitable(result):
                return _settle(self, result, returns, pol)
            return _after(self, result)

        invoke.__webclient_op__ = True   # type: ignore[attr-defined]
        return invoke  # type: ignore[return-value]

    return decorate


def _settle(obj: Any, coro: Any, returns: str, pol: ErrorPolicy) -> Any:
    """A coroutine method: returned as an awaitable when a loop is already
    running in this thread (the evaluator, or an async caller -- they await
    it), else bridged onto the owning client's engine loop for a sync call.
    Either way the error policy applies to the awaited outcome."""
    async def guarded() -> Any:
        try:
            result = await coro
        except Exception as exc:              # noqa: BLE001 -- policy decides
            return _fail(obj, returns, exc, pol)
        return _after(obj, result)

    try:
        asyncio.get_running_loop()
        return guarded()                      # on a running loop: caller awaits
    except RuntimeError:
        return _client_of(obj)._ensure_loop().run(guarded())   # sync: bridge


def _tag(exc: BaseException) -> BaseException:
    """Mark an exception raised under an explicit RAISE so every enclosing
    policy re-raises it -- an explicit RAISE aborts the whole plan."""
    try:
        exc._webclient_raise = True     # type: ignore[attr-defined]
    except Exception:
        pass
    return exc


def _fail(obj: Any, returns: str, exc: Exception, pol: ErrorPolicy) -> Any:
    if pol == RAISE or getattr(exc, "_webclient_raise", False):
        raise _tag(exc)
    error = exc.error if isinstance(exc, OpError) and exc.error else \
        WebError.from_exception(exc)
    text = f"{error.type}: {error.message}"
    produced = getattr(exc, "document", None)     # a fetch that answered
    if returns == "None":                         # non-WebBase-returning
        return None if pol == IGNORE else error
    if pol == IGNORE:
        return _carry(obj, returns, error, f"Ignored {text}", ok=True,
                      produced=produced)
    return _carry(obj, returns, error, text, ok=False, produced=produced)


def _carry(obj: Any, returns: str, error: Any, message: str | None, *,
           ok: bool, produced: Any = None) -> Any:
    if produced is not None and hasattr(produced, "ok"):
        # a fetch that answered: keep the document's own ok/error (e.g. the
        # HTTPStatus of a 404); IGNORE only flips it to ok.
        if ok:
            produced.ok, produced.updated = True, now()
        return produced
    if returns == "Self":
        target = obj
    else:
        target = CLASSES[returns].model_construct(root=obj.name or None)
        target._client, target._session = obj._client, obj._session
    target.error, target.message, target.ok = error, message, ok
    target.updated = now()
    return target


def _after(obj: Any, result: Any) -> Any:
    """Stamp identity on a fresh WebBase result and register it in scope; a
    Self op that succeeded on an ok receiver clears any stale error."""
    if result is obj:
        obj.error, obj.message, obj.ok, obj.updated = None, None, True, now()
        return result
    webbase = CLASSES.get("WebBase")
    if webbase is not None and isinstance(result, webbase):
        if not result.root and obj.name:
            result.root = obj.name
        if result._client is None:
            result._client, result._session = obj._client, obj._session
        if not result.name and result._registrable and result._client:
            result._client._scope_for(result._session).add(result)
    return result


def _client_of(obj: Any) -> Any:
    client = getattr(obj, "_client", None)
    if client is None:
        from .engine import default_client
        client = default_client()
    return client


__all__ = ["ErrorPolicy", "Capability", "IGNORE", "RETURN", "RAISE", "OpError",
           "UnsupportedOperation", "policy", "default_policy", "now", "CLASSES",
            "NameScope"]
