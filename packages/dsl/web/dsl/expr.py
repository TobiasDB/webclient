"""Expr: the lazy recorder -- core-agnostic, records into a :class:`~web.dsl.plan.Plan`.

Every attribute access, call or operator on an ``Expr`` returns a new ``Expr`` extending its plan;
nothing runs until a terminal (:meth:`Expr.collect` / :meth:`Expr.acollect`) walks it through the
executor (:mod:`web.dsl.run`). The recorder knows nothing about the cores -- the one safety
boundary is that a ``_``-prefixed name is never recordable. Static types come from the lazy
surfaces the roots are cast to (:mod:`web.dsl.surface`); at runtime every value in a chain is an
``Expr``. Ported from the monolith so both versions record ONE plan language.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Iterator, NoReturn, TypeVar, cast

from pydantic import JsonValue

from .plan import Arg, Plan, Step
from .run import arun

if TYPE_CHECKING:
    from web.resolve import Resolver

T = TypeVar("T")


def _run_sync(coro: "Coroutine[object, object, object]") -> object:
    """Run a coroutine to completion and return its result, callable from ANY context: with no
    running loop it is a plain ``asyncio.run``; called from INSIDE a running loop it runs on a
    private loop in a worker thread and blocks for the result. A worker thread (not
    ``run_coroutine_threadsafe``) is required: that submits to a loop on ANOTHER thread, but here the
    loop is on THIS thread and busy running the caller, so submitting + waiting on it would deadlock.
    So ``collect`` is genuinely synchronous everywhere (from async code, prefer ``acollect`` to avoid
    blocking the loop)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # the common case: no loop here
    out: "list[object]" = []
    err: "list[BaseException]" = []
    worker = threading.Thread(target=lambda: _drain(coro, out, err))
    worker.start()
    worker.join()
    if err:
        raise err[0]
    return out[0]


def _drain(
    coro: "Coroutine[object, object, object]",
    out: "list[object]",
    err: "list[BaseException]",
) -> None:
    """The worker body: run ``coro`` on its own loop, capturing the result or the exception."""
    try:
        out.append(asyncio.run(coro))
    except BaseException as exc:  # re-raised on the calling thread
        err.append(exc)


class _Missing:
    pass


_MISSING: object = _Missing()


class Expr:
    """A recorded chain rooted at a :class:`~web.dsl.plan.Plan`, optionally BOUND to a resolver (the
    context-managed :class:`~web.dsl.facade.WebClient` entry binds its resolver so a chain's
    terminals reuse it); an unbound chain uses a transient resolver at the terminal."""

    __slots__ = ("_plan", "_bound")
    _plan: Plan  # declared so mypy reads this, not the recording __getattr__
    _bound: "Resolver | None"

    def __init__(self, plan: Plan, bound: "Resolver | None" = None) -> None:
        object.__setattr__(self, "_plan", plan)
        object.__setattr__(self, "_bound", bound)

    # -- recording -----------------------------------------------------------
    def _extend(self, step: Step) -> "Expr":
        """A new ``Expr`` with ``step`` appended (the core recording move), keeping the binding."""
        return Expr(self._plan.extend(step), self._bound)

    def __getattr__(self, name: str) -> "Expr":
        """Record attribute access as a ``get`` step. Underscore names are the safety boundary."""
        if name.startswith("_"):  # the one safety boundary
            raise AttributeError(name)
        return self._extend(Step(kind="get", name=name))

    def __call__(self, *args: object, **kwargs: object) -> "Expr":
        """Record a call step (its args/kwargs captured as plan args)."""
        return self._extend(
            Step(
                kind="call",
                args=[to_arg(a) for a in args],
                kwargs={k: to_arg(v) for k, v in kwargs.items()},
            )
        )

    def _op(self, name: str, other: object = _MISSING) -> "Expr":
        """Record a binary/unary operator as an ``op`` step -- the shared builder behind the dunders."""
        args = [] if other is _MISSING else [to_arg(other)]
        return self._extend(Step(kind="op", name=name, args=args))

    def __eq__(self, o: object) -> "Expr":  # type: ignore[override]
        return self._op("eq", o)

    def __ne__(self, o: object) -> "Expr":  # type: ignore[override]
        return self._op("ne", o)

    def __lt__(self, o: object) -> "Expr":
        return self._op("lt", o)

    def __le__(self, o: object) -> "Expr":
        return self._op("le", o)

    def __gt__(self, o: object) -> "Expr":
        return self._op("gt", o)

    def __ge__(self, o: object) -> "Expr":
        return self._op("ge", o)

    def __and__(self, o: object) -> "Expr":
        return self._op("and", o)

    def __or__(self, o: object) -> "Expr":
        return self._op("or", o)

    def __invert__(self) -> "Expr":
        return self._op("not")

    __hash__ = None  # type: ignore[assignment]

    def _coerce(self, what: str) -> NoReturn:
        """The shared error for a Python coercion (truth/len/iter) attempted on a lazy expr."""
        raise TypeError(
            f"a lazy expression has no {what}: it records, it does not run. "
            "Use it inside extract(...) / filter(...) or with `& | ~`."
        )

    def __bool__(self) -> bool:
        return self._coerce("truth value")

    def __len__(self) -> int:
        return self._coerce("length")

    def __iter__(self) -> "Iterator[object]":
        return self._coerce("iterator")

    # -- evaluation: the four dispatch modes ---------------------------------
    async def acollect(self, root: object = None, *, resolver: "Resolver | None" = None) -> object:
        """ASYNC dispatch: walk the plan on the caller's loop and return the materialised result --
        smart about its shape (extracted rows -> ``list[dict]``, a field chain -> a list of values,
        a single field -> its value). ``root`` roots a context plan (a URL / Document); a
        ``reference(url)`` plan needs none. ``resolver`` fetches (a transient one is used + closed
        when omitted)."""
        return await arun(self._plan, root, resolver=resolver or self._bound)

    def collect(self, root: object = None, *, resolver: "Resolver | None" = None) -> object:
        """SYNC dispatch: :meth:`acollect` run to completion (blocks). Works from any context --
        plain code or inside a running loop (see :func:`_run_sync`)."""
        return _run_sync(self.acollect(root, resolver=resolver))

    def to_blob(self) -> str:
        """SERVICE/API dispatch: this expression's plan as a portable blob (see
        :meth:`~web.dsl.plan.Plan.to_blob`); rebuild + run it with :func:`~web.dsl.run.run_blob`.
        """
        return self._plan.to_blob()

    def describe(self) -> str:
        """A readable one-line rendering of the recorded chain (round-trippable via the blob)."""
        return self._plan.describe()

    def __repr__(self) -> str:
        return f"lazy {self._plan.describe()}"


def to_arg(value: object) -> Arg:
    """Wrap a call/operator argument as a plan ``Arg``: a nested ``Expr`` becomes a sub-plan arg
    (evaluated per element at run time), any other value a literal arg."""
    if isinstance(value, Expr):
        return Arg(plan=value._plan)
    return Arg(value=cast(JsonValue, value))  # a literal arg must be JSON (it rides the wire blob)


def lazy(cls: type[T], *, plan: Plan | None = None) -> T:
    """A recording root for ``cls`` -- statically ``cls``, at runtime an ``Expr``."""
    return cast(T, Expr(plan or Plan(root=cls.__name__)))


def from_plan(plan: "Plan | dict[str, object] | str") -> Expr:
    """Rebuild an ``Expr`` from its wire form -- a ``Plan``, its dict, or a ``to_blob`` string --
    validating its names first (the wire safety boundary for the service/remote modes).
    """
    if isinstance(plan, str):
        plan = Plan.from_blob(plan)
    elif isinstance(plan, dict):
        plan = Plan.model_validate(plan)
    plan.validate_names()
    return Expr(plan)


def from_blob(blob: str) -> Expr:
    """Rebuild an ``Expr`` from a :meth:`~web.dsl.plan.Plan.to_blob` string, validated -- the
    LLM-authoring path: write a plan, encode it, rebuild + validate + pretty-print before running.
    """
    return from_plan(blob)


__all__ = ["Expr", "lazy", "from_plan", "from_blob", "to_arg"]
