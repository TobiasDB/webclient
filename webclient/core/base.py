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
    from .models import Document, Reference

F = TypeVar("F", bound=Callable[..., Any])

ErrorPolicy = Literal["error-ignore", "error-return", "error-raise"]
IGNORE: ErrorPolicy = "error-ignore"   # ok=True, error kept for diagnostics
RETURN: ErrorPolicy = "error-return"   # ok=False, error set, object returned
RAISE: ErrorPolicy = "error-raise"     # raise where you stand

Capability = Literal["tree", "page", "ok"]

#: the default when a call carries no explicit ``error=``. Eager = RAISE;
#: the evaluator sets RETURN around a plan so one bad row cannot abort it.
_DEFAULT: ContextVar[ErrorPolicy] = ContextVar("default_policy", default=RAISE)

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
                have = self._capabilities()
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
    if isinstance(result, WebBase):
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


# ========================================================================= #
# Engine-core base: shared loop lifecycle for the local and remote cores
# ========================================================================= #

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


# ========================================================================= #
# Surface base types (WebBase / WebError / Field / Collection)
# ========================================================================= #

class WebError(BaseModel):
    """A serializable failure carried by a WebBase -- never a bare Exception
    on the wire."""

    type: str
    message: str
    detail: dict[str, Any] = {}

    @classmethod
    def from_exception(cls, exc: BaseException) -> WebError:
        return cls(type=type(exc).__name__, message=str(exc))


def _plain(value: Any) -> Any:
    """Projection of one extracted value: Fields unwrap (None when not ok),
    Documents/Collections project recursively, References stay References."""
    if isinstance(value, Field):
        return value.value if value.ok else None
    if isinstance(value, Collection):
        return value.project()
    doc_cls = CLASSES.get("Document")
    if doc_cls is not None and isinstance(value, doc_cls):
        return value.project()
    return value


class WebBase(BaseModel):
    """Addressable, error-carrying base of every surface object. Names/roots
    are assigned by the resolver that registers the object (Decision 9)."""

    name: str = ""                  # short, low-entropy: doc:000-001
    kind: str = ""
    root: str | None = None         # name of the object this derives from
    created: int = 0
    updated: int = 0
    accessed: int = 0
    error: WebError | None = None
    message: str | None = None
    ok: bool = True                 # the ONLY truth; `error` is diagnostics

    _registrable: ClassVar[bool] = True
    _client: Any = PrivateAttr(default=None)        # owning WebClient
    _session: Any = PrivateAttr(default=None)
    _fields: dict[str, Any] = PrivateAttr(default_factory=dict)

    def __repr_args__(self) -> Any:
        yield from ((k, getattr(self, k)) for k in ("name", "kind")
                    if getattr(self, k))
        yield "ok", self.ok

    def _capabilities(self) -> frozenset[Capability]:
        return frozenset({"ok"}) if self.ok else frozenset()

    def _is_empty(self) -> bool:
        return not self._fields

    # -- extraction -----------------------------------------------------------
    @policy(returns="Self")
    def extract(self, *, error: ErrorPolicy | None = None,
                **named_expr: Any) -> Self:
        """Evaluate each expression against this object and store it under its
        name; later names see earlier ones (Decision 12/13)."""
        return self._aextract(named_expr)  # type: ignore[return-value]  # @policy settles

    async def _aextract(self, named_expr: dict[str, Any]) -> Self:
        from .executor import evaluate
        with default_policy(RETURN):            # a missing field is None, not fatal
            for name, expr in named_expr.items():
                self._fields[name] = await evaluate(expr, self)
        return self

    def project[T](self, model: type[T] | None = None, *, error: ErrorPolicy | None = None) -> T | dict[str, Any]:
        """Terminal: the extracted fields as a nested dict (or ``model``)."""
        if not self.ok:
            raise OpError(self.error)
        data = {k: _plain(v) for k, v in self._fields.items()}
        return model(**data) if model is not None else data  # type: ignore[call-arg]

    @policy(returns="Field", always=True)
    def is_empty(self, *, error: ErrorPolicy | None = None) -> Field[bool]:
        return Field[bool](value=self._is_empty())

    @policy(returns="Field", always=True)
    def is_ok(self, *, error: ErrorPolicy | None = None) -> Field[bool]:
        return Field[bool](value=self.ok)

    # -- accessors for extracted values ---------------------------------------
    def _extracted(self, name: str, kind: type | None = None) -> Any:
        if name not in self._fields:
            raise LookupError(f"no extracted value {name!r} on {self!r}")
        value = self._fields[name]
        if kind is not None and not isinstance(value, kind):
            raise TypeError(f"{name!r} is a {type(value).__name__}, "
                            f"not a {kind.__name__}")
        return value

    @policy(returns="Field")
    def field(self, name: str, *, error: ErrorPolicy | None = None) -> Field[Any]:
        value = self._extracted(name)
        return value if isinstance(value, Field) else Field[Any](value=value)

    def fields(self, *names: str) -> dict[str, Any]:
        return {n: _plain(self._extracted(n)) for n in names or self._fields}

    @policy(returns="Reference")
    def reference(self, name: str, *, error: ErrorPolicy | None = None) -> Reference:
        return cast("Reference", self._extracted(name, CLASSES["Reference"]))

    @policy(returns="Collection")
    def references(self, *names: str, error: ErrorPolicy | None = None) -> Collection[Reference]:
        return _gather(self, names, CLASSES["Reference"])

    @policy(returns="Document")
    def document(self, name: str, *, error: ErrorPolicy | None = None) -> Document:
        return cast("Document", self._extracted(name, CLASSES["Document"]))

    @policy(returns="Collection")
    def documents(self, *names: str, error: ErrorPolicy | None = None) -> Collection[Document]:
        return _gather(self, names, CLASSES["Document"])


def _gather(obj: WebBase, names: tuple[str, ...], kind: type) -> Any:
    """The named extracted values as one Collection; a value that is itself a
    Collection contributes its elements."""
    items: list[Any] = []
    for name in names or obj._fields:
        value = obj._extracted(name)
        found = list(value) if isinstance(value, Collection) else [value]
        bad = [v for v in found if not isinstance(v, kind)]
        if bad:
            raise TypeError(f"{name!r} holds a {type(bad[0]).__name__}, "
                            f"not a {kind.__name__}")
        items.extend(found)
    out: Collection[Any] = Collection()
    out._items = items
    return out


# --------------------------------------------------------------------------- #
# Field
# --------------------------------------------------------------------------- #

class Field[T](WebBase):
    """A scalar produced by a method. Comparisons and ``& | ~`` return a
    ``Field[bool]`` (so they work eagerly and, on a lazy ``Expr``, record).
    Never registered by name (Decision 3)."""

    value: Any = None

    _registrable: ClassVar[bool] = False
    _cond: bool | None = PrivateAttr(default=None)   # when(...) state

    def get(self) -> T:
        if not self.ok:
            raise OpError(self.error)
        return cast(T, self.value)

    def _is_empty(self) -> bool:
        return self.value is None or self.value in ("", [], {}, b"")

    def _cmp(self, name: str, other: object) -> Field[bool]:
        mine = self.get()
        theirs = other.get() if isinstance(other, Field) else other
        table: dict[str, Any] = {
            "eq": mine == theirs, "ne": mine != theirs,
            "lt": mine < theirs, "le": mine <= theirs,      # type: ignore[operator]
            "gt": mine > theirs, "ge": mine >= theirs,      # type: ignore[operator]
            "and": bool(mine) and bool(theirs),
            "or": bool(mine) or bool(theirs), "not": not mine}
        return Field[bool](value=table[name])

    def __eq__(self, o: object) -> Field[bool]: return self._cmp("eq", o)  # type: ignore[override]
    def __ne__(self, o: object) -> Field[bool]: return self._cmp("ne", o)  # type: ignore[override]
    def __lt__(self, o: object) -> Field[bool]: return self._cmp("lt", o)
    def __le__(self, o: object) -> Field[bool]: return self._cmp("le", o)
    def __gt__(self, o: object) -> Field[bool]: return self._cmp("gt", o)
    def __ge__(self, o: object) -> Field[bool]: return self._cmp("ge", o)
    def __and__(self, o: object) -> Field[bool]: return self._cmp("and", o)
    def __or__(self, o: object) -> Field[bool]: return self._cmp("or", o)
    def __invert__(self) -> Field[bool]: return self._cmp("not", None)

    __hash__ = None  # type: ignore[assignment]

    def __bool__(self) -> bool:
        return bool(self.get())

    # -- branching (Decision 5) ----------------------------------------------
    def when(self, cond: Field[bool] | bool) -> Field[T]:
        out = Field[Any](value=self.value)
        out._cond = bool(cond.get() if isinstance(cond, Field) else cond)
        return out

    def then(self, value: T | Field[T]) -> Field[T]:
        out = Field[Any](value=self.value)
        out._cond = self._cond
        if self._cond:
            out.value = value.get() if isinstance(value, Field) else value
        return out

    def otherwise(self, value: T | Field[T]) -> Field[T]:
        out = Field[Any](value=self.value)
        if self._cond is False:
            out.value = value.get() if isinstance(value, Field) else value
        return out


# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #

class Collection[T](WebBase):
    """One address for many elements (Decision 10). Element ops (``select``,
    ``attr``, ``resolve``, ...) map over the elements; the whole-collection
    ops are ``_WHOLE``. Iteration is eager-only."""

    _items: list[Any] = PrivateAttr(default_factory=list)
    #: ops that act on the collection itself; everything else maps per element
    _WHOLE: ClassVar[frozenset[str]] = frozenset(
        {"extract", "filter", "project", "is_ok", "is_empty"})

    def _is_empty(self) -> bool:
        return not self._items

    @policy(returns="Field", always=True)
    def is_empty(self, *, error: ErrorPolicy | None = None) -> Field[bool]:
        return Field[bool](value=self._is_empty())

    @policy(returns="Field", always=True)
    def is_ok(self, *, error: ErrorPolicy | None = None) -> Field[bool]:
        return Field[bool](value=self.ok)

    @policy(returns="Self")
    def extract(self, *, error: ErrorPolicy | None = None,  # type: ignore[override]
                **named_expr: Any) -> Collection[T]:
        return self._aextract_all(named_expr)  # type: ignore[return-value]

    async def _aextract_all(self, named_expr: dict[str, Any]) -> Collection[T]:
        for el in self._items:
            await el.extract(**named_expr)
        return self

    @policy(returns="Collection")
    def filter(self, *expr: Any, error: ErrorPolicy | None = None,
               **named_expr: Any) -> Collection[T]:
        """Keep the elements for which every expression is truthy (a not-ok
        result is falsy)."""
        return self._afilter((*expr, *named_expr.values()))  # type: ignore[return-value]

    async def _afilter(self, exprs: tuple[Any, ...]) -> Collection[T]:
        from .executor import _truthy, evaluate
        kept: list[Any] = []
        with default_policy(RETURN):            # a not-ok predicate is just falsy
            for el in self._items:
                results = [await evaluate(e, el) for e in exprs]
                if all(_truthy(r) for r in results):
                    kept.append(el)
        out: Collection[T] = Collection()
        out._items = kept
        return out

    def project[M](self, model: type[M] | None = None, *, error: ErrorPolicy | None = None) -> list[M | dict[str, Any]]:  # type: ignore[override]
        return [el.project(model) for el in self._items]

    def __iter__(self) -> Iterator[T]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    if not TYPE_CHECKING:
        def __getattr__(self, name: str) -> Any:
            """Eager element-op lifting: an op not defined on Collection maps
            over the elements (the evaluator does the same in a plan).
            Flattens when an element op returns a Collection."""
            if name.startswith("_"):
                return super().__getattr__(name)   # pydantic private attrs
            if name in type(self)._WHOLE:
                raise AttributeError(name)

            def lifted(*args: Any, **kwargs: Any) -> Any:
                items: list[Any] = []
                for el in self._items:
                    r = getattr(el, name)(*args, **kwargs)
                    items.extend(r) if isinstance(r, Collection) else items.append(r)
                if items and not all(isinstance(x, WebBase) for x in items):
                    return items          # plain values (e.g. projected dicts)
                out: Collection[Any] = Collection(root=self.name or None)
                out._items = items
                out._client, out._session = self._client, self._session
                return out
            return lifted

    if TYPE_CHECKING:
        # >>> generated by scripts/gen_stubs.py -- do not edit
        @overload  # type: ignore[overload-overlap]
        def attr(self, name: Literal['href', 'src', 'action'], *, error: ErrorPolicy | None = None) -> Collection[Reference]: ...
        @overload
        def attr(self, name: str, *, error: ErrorPolicy | None = None) -> Collection[Field[str]]: ...
        def attr(self, name: str, *, error: ErrorPolicy | None = None) -> Any: ...  # type: ignore[empty-body]
        def check(self, selector: str, checked: bool = True, *, error: ErrorPolicy | None = None, **kw: Any) -> Collection[T]: ...  # type: ignore[empty-body]
        def click(self, selector: str | None = None, *, error: ErrorPolicy | None = None, **kw: Any) -> Collection[T]: ...  # type: ignore[empty-body]
        def document(self, name: str, *, error: ErrorPolicy | None = None) -> Collection[Any]: ...  # type: ignore[empty-body]
        def documents(self, *names: str, error: ErrorPolicy | None = None) -> Collection[Document]: ...  # type: ignore[empty-body]
        def drag(self, source: str, target: str, *, error: ErrorPolicy | None = None) -> Collection[T]: ...  # type: ignore[empty-body]
        def evaluate(self, script: str, *, error: ErrorPolicy | None = None) -> Collection[Any]: ...  # type: ignore[empty-body]
        def execute(self, script: str, *, error: ErrorPolicy | None = None) -> Collection[T]: ...  # type: ignore[empty-body]
        def field(self, name: str, *, error: ErrorPolicy | None = None) -> Collection[Field[Any]]: ...  # type: ignore[empty-body]
        def hover(self, selector: str, *, error: ErrorPolicy | None = None, **kw: Any) -> Collection[T]: ...  # type: ignore[empty-body]
        def press(self, key: str, *, error: ErrorPolicy | None = None, **kw: Any) -> Collection[T]: ...  # type: ignore[empty-body]
        def reference(self, name: str, *, error: ErrorPolicy | None = None) -> Collection[Any]: ...  # type: ignore[empty-body]
        def references(self, *names: str, error: ErrorPolicy | None = None) -> Collection[Reference]: ...  # type: ignore[empty-body]
        def render(self, format: str, **options: Any) -> Collection[Any]: ...  # type: ignore[empty-body]
        def resolve(self, *, browser: bool = False, session: Any = None, optional: bool = False, error: ErrorPolicy | None = None, **options: Any) -> Collection[Document]: ...  # type: ignore[empty-body]
        def screenshot(self, selector: str | None = None, *, error: ErrorPolicy | None = None, **kw: Any) -> Collection[Document]: ...  # type: ignore[empty-body]
        def scroll(self, selector: str | None = None, *, x: int = 0, y: int = 0, error: ErrorPolicy | None = None) -> Collection[T]: ...  # type: ignore[empty-body]
        def select(self, selector: str, *, index: int = 0, wait: float | None = None, error: ErrorPolicy | None = None, optional: bool = False) -> Collection[Document]: ...  # type: ignore[empty-body]
        def select_all(self, selector: str, limit: int | None = None, offset: int = 0, *, error: ErrorPolicy | None = None) -> Collection[Document]: ...  # type: ignore[empty-body]
        def select_option(self, selector: str, *, error: ErrorPolicy | None = None, **kw: Any) -> Collection[T]: ...  # type: ignore[empty-body]
        def upload(self, selector: str, files: Sequence[str], *, error: ErrorPolicy | None = None) -> Collection[T]: ...  # type: ignore[empty-body]
        def wait_for(self, selector: str | None = None, *, error: ErrorPolicy | None = None, **kw: Any) -> Collection[T]: ...  # type: ignore[empty-body]
        def write(self, selector: str, text: str, *, error: ErrorPolicy | None = None, **kw: Any) -> Collection[T]: ...  # type: ignore[empty-body]
        # <<< generated
        pass


# ------------------------------------------------------------------------- #
# Name scope (scoped id registry with retention, Decision 9)
# ------------------------------------------------------------------------- #

_PREFIX = {"Reference": "ref", "Document": "doc", "Collection": "col"}


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


__all__ = ["ErrorPolicy", "Capability", "IGNORE", "RETURN", "RAISE", "OpError",
           "UnsupportedOperation", "policy", "default_policy", "now", "CLASSES",
           "WebBase", "WebError", "Field", "Collection", "NameScope"]
