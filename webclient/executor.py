"""The evaluator: one async walk over a recorded ``Plan``.

The core is async (``aevaluate`` / ``astream``); it drives the eager surface by
``getattr`` and ``await``s any op that returns a coroutine (the IO ops --
``resolve`` -- hand back a coroutine when already on the engine loop). A
Collection fans out per element through the bounded ``fan_out``. Sync callers
use ``evaluate`` (a ``loop.run`` bridge); the async client awaits ``aevaluate``
on the engine loop; streaming yields rows as they complete.
"""

from __future__ import annotations

import asyncio
import operator
from typing import Any, AsyncIterator, Awaitable, Callable

from .expr import Expr
from .plan import Arg, Step

DEFAULT_FANOUT = 8

_OPS = {
    "eq": operator.eq,
    "ne": operator.ne,
    "lt": operator.lt,
    "le": operator.le,
    "gt": operator.gt,
    "ge": operator.ge,
    "and": operator.and_,
    "or": operator.or_,
}

#: bound ops: their expression args are passed unevaluated (evaluated per
#: element inside the op); the executor calls their async ``a<name>`` form.
_BINDS = {"extract", "filter"}
#: ops acting on a Collection as a whole (everything else fans out per element)
_COLL_OPS = {"extract", "filter", "project", "limit", "documents"}


def _iscoro(value: Any) -> bool:
    return asyncio.iscoroutine(value)


def _fanout_limit(client: Any) -> int:
    pool = getattr(client, "_pool", None)
    limits = getattr(pool, "_limits", None) if pool is not None else None
    return (limits or {}).get("http", DEFAULT_FANOUT)


def _row_of(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    core = getattr(value, "_core", None)
    return getattr(core, "_row", None) if core is not None else None


def truthy(value: Any) -> bool:
    """Whether an evaluated value counts as true (a not-ok surface/field is
    false; otherwise normal truthiness)."""
    from .collection import Field

    if isinstance(value, Field):
        return bool(value)
    if hasattr(value, "ok"):
        return bool(value.ok) and bool(value)
    return bool(value)


# -- async core --------------------------------------------------------------


async def aevaluate(expr: Any, context: Any = None, *, client: Any = None) -> Any:
    """Evaluate ``expr`` against ``context`` (async). A non-Expr value is itself."""
    if not isinstance(expr, Expr):
        return expr
    client = client or expr._client or getattr(context, "_client", None)
    if isinstance(context, Expr):  # an Expr context (wc.ref(url)) runs first
        context = await aevaluate(context, client=client)
    value = _start(expr._plan, context, client)
    return await _arun(value, expr._plan.steps, 0, context, client)


async def _arun(
    value: Any, steps: list[Step], i: int, context: Any, client: Any
) -> Any:
    from .collection import Collection

    while i < len(steps):
        step = steps[i]
        if isinstance(value, Collection) and not (
            step.kind == "get" and step.name in _COLL_OPS
        ):
            rest = steps[i:]
            results = await fan_out(
                list(value),
                lambda el: _arun(el, rest, 0, el, client),
                limit=_fanout_limit(client),
            )
            if results and all(hasattr(r, "_core") for r in results):
                return Collection(results, client=client, root=value.root)
            return results
        value, i = await _aapply(value, steps, i, context, client)
    return value


async def _aapply(
    value: Any, steps: list[Step], i: int, context: Any, client: Any
) -> tuple[Any, int]:
    step = steps[i]
    if step.kind == "get":
        nxt = steps[i + 1] if i + 1 < len(steps) else None
        if nxt is not None and nxt.kind == "call":
            return await _acall(value, step.name, nxt, context, client), i + 2
        return getattr(value, step.name), i + 1
    if step.kind == "op":
        other = await _aarg(step.args[0], context, client) if step.args else None
        return _OPS[step.name](value, other), i + 1
    if step.kind == "when":
        cond, then_arg, else_arg = step.args
        chosen = then_arg if truthy(await _aarg(cond, context, client)) else else_arg
        return await _aarg(chosen, context, client), i + 1
    if step.kind == "fn":  # is_empty(x) == x.is_empty()
        op = getattr(value, step.name, None)
        result = op() if callable(op) else op
        if _iscoro(result):
            from .surface import wrap

            result = wrap(await result)
        return result, i + 1
    raise ValueError(f"cannot evaluate step {step.kind!r}")


async def _acall(value: Any, name: str, call: Step, context: Any, client: Any) -> Any:
    # field(k) / reference(k) read an extracted column off the element's _row
    if name in ("field", "reference"):
        row = _row_of(value)
        if row is not None:
            column = row.get(await _aarg(call.args[0], context, client))
            if name == "field":
                from .collection import Field

                return column if isinstance(column, Field) else Field(column)
            return column
    if name in _BINDS:  # sub-plans passed unevaluated to the async bound op
        args = [_as_expr(a, client) for a in call.args]
        kwargs = {k: _as_expr(v, client) for k, v in call.kwargs.items()}
        return await getattr(value, "a" + name)(*args, **kwargs)
    args = [await _aarg(a, context, client) for a in call.args]
    kwargs = {k: await _aarg(v, context, client) for k, v in call.kwargs.items()}
    result = getattr(value, name)(*args, **kwargs)
    if _iscoro(result):  # an IO op (resolve): await, then wrap the core it yields
        from .surface import wrap

        return wrap(await result)
    return result


def _as_expr(arg: Arg, client: Any) -> Any:
    return Expr(arg.plan, client) if arg.plan is not None else arg.value


async def _aarg(arg: Arg, context: Any, client: Any) -> Any:
    if arg.plan is None:
        return arg.value
    return await aevaluate(Expr(arg.plan, client), context, client=client)


def _start(plan: Any, context: Any, client: Any) -> Any:
    """The value a plan starts from: a reconstructed Reference (source plan) or
    the passed context (doc/ref/field roots)."""
    if plan.source is not None and "document_id" not in plan.source:
        from .core.reference_core import ReferenceCore
        from .core.session_core import WebSessionCore
        from .surface import wrap

        core = ReferenceCore(**plan.source)
        if isinstance(context, WebSessionCore):  # a session-bound reference
            core._session = context
            core._client = context
        else:
            core._client = client
        return wrap(core)
    if context is None and plan.root:
        raise ValueError(
            f"a plan rooted at {plan.root} needs a context; pass one to "
            "execute()/collect() or root it with reference(url)"
        )
    return context


# -- streaming ---------------------------------------------------------------


async def astream(
    expr: Any, context: Any = None, *, client: Any = None
) -> AsyncIterator[Any]:
    """Yield the plan's rows as they complete (a terminal ``project`` list, or a
    single value, streamed one at a time)."""
    result = await aevaluate(expr, context, client=client)
    rows = result if isinstance(result, list) else [result]
    for row in rows:
        yield row


# -- sync bridge -------------------------------------------------------------


def evaluate(expr: Any, context: Any = None, *, client: Any = None) -> Any:
    """Sync entry: run ``aevaluate`` on the client's engine loop."""
    if not isinstance(expr, Expr):
        return expr
    client = client or expr._client or getattr(context, "_client", None)
    from .core.client_core import WebClientCore

    engine = client if client is not None else WebClientCore()
    return engine.loop().run(aevaluate(expr, context, client=client))


# -- bounded fan-out ---------------------------------------------------------


async def fan_out(
    items: list[Any], fn: Callable[[Any], Awaitable[Any]], *, limit: int
) -> list[Any]:
    """Run ``fn`` over ``items`` with at most ``limit`` in flight, results in
    input order. A failing task cancels its siblings and raises the first
    failure."""
    results: list[Any] = [None] * len(items)
    pending = iter(range(len(items)))

    async def worker() -> None:
        for i in pending:
            results[i] = await fn(items[i])

    try:
        async with asyncio.TaskGroup() as group:
            for _ in range(min(max(limit, 1), len(items)) or 1):
                group.create_task(worker())
    except BaseExceptionGroup as group_exc:  # unwrap to the first failure
        exc: BaseException = group_exc
        while isinstance(exc, BaseExceptionGroup):
            exc = exc.exceptions[0]
        raise exc from None
    return results


__all__ = ["aevaluate", "astream", "evaluate", "truthy", "fan_out"]
