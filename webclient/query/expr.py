"""Expr: the lazy recorder -- Core-agnostic, records into a ``Plan``.

Every attribute access, call or operator on an ``Expr`` returns a new ``Expr``
extending its plan; nothing runs until the executor walks it (via
``Expr.collect`` / ``WebClient.execute``). The recorder knows nothing about the
cores or surfaces -- the one safety boundary is that a ``_``-prefixed name is
never recordable. Static types come from the generated surface stubs the roots
are cast to (see ``scripts.gen_stubs``); at runtime every value in a chain is an
``Expr``.
"""

from __future__ import annotations

from typing import Any, NoReturn, TypeVar, cast

from .plan import Arg, Plan, Step

T = TypeVar("T")


class _Missing:
    pass


_MISSING: Any = _Missing()


class Expr:
    """A recorded chain rooted at a ``Plan`` (optionally bound to a client)."""

    __slots__ = ("_plan", "_client", "_context")
    _plan: Plan  # declared so mypy reads these, not the recording __getattr__
    _client: Any
    _context: Any  # a materialised surface this recorder is bound to (doc.lazy)

    def __init__(self, plan: Plan, client: Any = None, context: Any = None) -> None:
        object.__setattr__(self, "_plan", plan)
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_context", context)

    # -- recording -----------------------------------------------------------
    def _extend(self, step: Step) -> "Expr":
        return Expr(self._plan.extend(step), self._client, self._context)

    def __getattr__(self, name: str) -> "Expr":
        if name.startswith("_"):  # the one safety boundary
            raise AttributeError(name)
        return self._extend(Step(kind="get", name=name))

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        eager = kwargs.pop("_collect", False)  # per-call eager escape hatch
        nxt = self._extend(
            Step(
                kind="call",
                args=[to_arg(a) for a in args],
                kwargs={k: to_arg(v) for k, v in kwargs.items()},
            )
        )
        return nxt.collect() if eager else nxt

    def _op(self, name: str, other: Any = _MISSING) -> "Expr":
        args = [] if other is _MISSING else [to_arg(other)]
        return self._extend(Step(kind="op", name=name, args=args))

    def __eq__(self, o: Any) -> "Expr":  # type: ignore[override]
        return self._op("eq", o)

    def __ne__(self, o: Any) -> "Expr":  # type: ignore[override]
        return self._op("ne", o)

    def __lt__(self, o: Any) -> "Expr":
        return self._op("lt", o)

    def __le__(self, o: Any) -> "Expr":
        return self._op("le", o)

    def __gt__(self, o: Any) -> "Expr":
        return self._op("gt", o)

    def __ge__(self, o: Any) -> "Expr":
        return self._op("ge", o)

    def __and__(self, o: Any) -> "Expr":
        return self._op("and", o)

    def __or__(self, o: Any) -> "Expr":
        return self._op("or", o)

    def __invert__(self) -> "Expr":
        return self._op("not")

    __hash__ = None  # type: ignore[assignment]

    def _coerce(self, what: str) -> NoReturn:
        raise TypeError(
            f"a lazy expression has no {what}: it records, it does not run. "
            "Use it inside extract(...) / filter(...) or with `& | ~` -- not "
            "and/or/not/bool/len/iter."
        )

    def __bool__(self) -> bool:
        return self._coerce("truth value")

    def __len__(self) -> int:
        return self._coerce("length")

    def __iter__(self) -> Any:
        return self._coerce("iterator")

    # -- evaluation ----------------------------------------------------------
    #: the single realization path: collect/acollect/stream/astream run on the
    #: bound client (or the context's) via its ``execute`` machinery -- a remote
    #: client round-trips over HTTP, all the same call. Users never call a
    #: client's ``execute`` directly. These names are reserved (non-recordable).
    def _ctx(self, context: Any) -> Any:
        return context if context is not None else self._context

    def _client_for(self, context: Any) -> Any:
        client = self._client or getattr(context, "_client", None)
        if client is None:
            from ..core.client import WebClientCore

            client = WebClientCore()  # process-local default (MVP)
        return client

    def collect(self, context: Any = None) -> Any:
        """Evaluate this plan and return the materialised result (sync)."""
        context = self._ctx(context)
        return self._client_for(context).execute(self, context)

    async def acollect(self, context: Any = None) -> Any:
        """The async twin of ``collect`` -- ``await lazy.acollect()`` -- runs on
        the engine loop without blocking the caller's loop."""
        context = self._ctx(context)
        return await self._client_for(context).aexecute(self, context)

    def stream(self, context: Any = None) -> Any:
        """Yield the plan's rows one at a time (sync iterator)."""
        context = self._ctx(context)
        return self._client_for(context).execute(self, context, stream=True)

    def astream(self, context: Any = None) -> Any:
        """Yield the plan's rows one at a time (async iterator)."""
        context = self._ctx(context)
        return self._client_for(context).astream(self, context)

    @property
    def is_lazy(self) -> bool:
        return True

    def __repr__(self) -> str:
        return f"lazy {self._plan.describe()}"


def to_arg(value: Any) -> Arg:
    if isinstance(value, Expr):
        return Arg(plan=value._plan)
    return Arg(value=value)


def lazy(cls: type[T], *, plan: Plan | None = None, client: Any = None) -> T:
    """A recording root for ``cls`` -- statically ``cls``, at runtime an
    ``Expr``."""
    return cast(T, Expr(plan or Plan(root=cls.__name__), client))


def lazy_root(core: Any) -> "Expr":
    """A lazy recorder rooted at a materialised surface ``core`` -- exposed as its
    ``.lazy`` property (see ``WebCore.lazy``).

    An engine core (client / session -- it has ``execute``) roots a ``WebClient``
    plan bound to itself, so ``wc.lazy.fetch(url).collect()`` records the verbs and
    runs them on that engine. Any other resolved surface (a document / reference)
    binds itself as the recorder's *context*, so ``doc.lazy.select(...).collect()``
    records a chain and runs it against that document."""
    if hasattr(core, "execute"):  # an engine core drives its own authoring verbs
        return Expr(Plan(root="WebClient"), client=core)
    return Expr(Plan(), client=getattr(core, "_client", None), context=core)


def from_plan(plan: Plan | dict[str, Any], client: Any = None) -> Expr:
    """Rebuild an ``Expr`` from its wire form (a Plan or its dict), validating
    its names first -- the wire safety boundary for the service/remote."""
    if isinstance(plan, dict):
        plan = Plan.model_validate(plan)
    plan.validate_names()
    return Expr(plan, client)


__all__ = [
    "Expr",
    "lazy",
    "lazy_root",
    "from_plan",
    "to_arg",
]
