"""The evaluator: one walk over a recorded ``Plan``.

A plan is walked step by step from a context, driving the *eager* surface
(``getattr`` on a ``Surface`` already resolves data fields / prop ops / call ops
through the core -- so the executor needs no separate op registry). ``extract``
and ``filter`` are "bound" ops: their sub-expression arguments are passed
unevaluated so the ``Collection`` can evaluate them per element.

MVP: synchronous, sequential fan-out. Async / bounded-pool / streaming are
later slices.
"""

from __future__ import annotations

import asyncio
import operator
from typing import Any, Awaitable, Callable

from .expr import Expr
from .plan import Arg, Step

DEFAULT_FANOUT = 8


def _row_of(value: Any) -> dict[str, Any] | None:
    """The extracted-columns dict for an element (a row dict is its own; a
    surface exposes its core's ``_row``); ``None`` if the value has no row."""
    if isinstance(value, dict):
        return value
    core = getattr(value, "_core", None)
    return getattr(core, "_row", None) if core is not None else None


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

#: ops whose expression arguments are evaluated per element inside the op
#: itself, so the executor passes them unevaluated (as ``Expr``).
_BINDS = {"extract", "filter"}
#: ops that act on a Collection as a whole (everything else fans out per element)
_COLL_OPS = {"extract", "filter", "project", "limit", "documents"}


def evaluate(expr: Any, context: Any = None, *, client: Any = None) -> Any:
    """Evaluate ``expr`` against ``context`` and return the result. A non-Expr
    value is itself."""
    if not isinstance(expr, Expr):
        return expr
    client = client or expr._client or getattr(context, "_client", None)
    if isinstance(context, Expr):  # an Expr context (wc.ref(url)) runs first
        context = evaluate(context, client=client)
    value = _start(expr._plan, context, client)
    return _run(value, expr._plan.steps, 0, context, client)


def _run(value: Any, steps: list[Step], i: int, context: Any, client: Any) -> Any:
    """Apply steps from ``i``. When the value is a Collection and the next step
    is not a whole-collection op, the rest of the chain fans out per element."""
    from .collection import Collection

    while i < len(steps):
        step = steps[i]
        if isinstance(value, Collection) and not (
            step.kind == "get" and step.name in _COLL_OPS
        ):
            rest = steps[i:]
            results = [_run(el, rest, 0, el, client) for el in value]
            if results and all(hasattr(r, "_core") for r in results):
                return Collection(results, client=client, root=value.root)
            return results
        value, i = _apply(value, steps, i, context, client)
    return value


def truthy(value: Any) -> bool:
    """Whether an evaluated value counts as true (a not-ok surface/field is
    false; otherwise normal truthiness)."""
    from .collection import Field

    if isinstance(value, Field):
        return bool(value)
    if hasattr(value, "ok"):
        return bool(value.ok) and bool(value)
    return bool(value)


# -- the walk ---------------------------------------------------------------


def _start(plan: Any, context: Any, client: Any) -> Any:
    """The value a plan starts from: a reconstructed Reference (source plan) or
    the passed context (doc/ref/field roots)."""
    if plan.source is not None and "document_id" not in plan.source:
        from .core.reference_core import ReferenceCore
        from .core.session_core import WebSessionCore
        from .surface import wrap

        core = ReferenceCore(**plan.source)
        if isinstance(context, WebSessionCore):  # a session-bound reference
            core._session = context  # resolve() will use the session
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


def _apply(
    value: Any, steps: list[Step], i: int, context: Any, client: Any
) -> tuple[Any, int]:
    step = steps[i]
    if step.kind == "get":
        nxt = steps[i + 1] if i + 1 < len(steps) else None
        if nxt is not None and nxt.kind == "call":
            return _call(value, step.name, nxt, context, client), i + 2
        return _read(value, step.name), i + 1
    if step.kind == "op":
        other = _arg(step.args[0], context, client) if step.args else None
        return _OPS[step.name](value, other), i + 1
    if step.kind == "when":
        cond, then_arg, else_arg = step.args
        chosen = then_arg if truthy(_arg(cond, context, client)) else else_arg
        return _arg(chosen, context, client), i + 1
    if step.kind == "fn":  # is_empty(x) == x.is_empty()
        op = getattr(value, step.name, None)
        return (op() if callable(op) else op), i + 1
    raise ValueError(f"cannot evaluate step {step.kind!r}")


def _read(value: Any, name: str) -> Any:
    """A property / prop-op / attribute read."""
    return getattr(value, name)


def _call(value: Any, name: str, call: Step, context: Any, client: Any) -> Any:
    # ``field(k)`` / ``reference(k)`` read an extracted column off the element
    # (its ``_row``) or off a plain row dict.
    if name in ("field", "reference"):
        row = _row_of(value)
        if row is not None:
            column = row.get(_arg(call.args[0], context, client))
            if name == "field":  # a field is a scalar leaf
                from .collection import Field

                return column if isinstance(column, Field) else Field(column)
            return column  # reference: the raw value
    if name in _BINDS:  # pass sub-plans unevaluated
        args = [_as_expr(a, client) for a in call.args]
        kwargs = {k: _as_expr(v, client) for k, v in call.kwargs.items()}
    else:
        args = [_arg(a, context, client) for a in call.args]
        kwargs = {k: _arg(v, context, client) for k, v in call.kwargs.items()}
    return getattr(value, name)(*args, **kwargs)


def _as_expr(arg: Arg, client: Any) -> Any:
    return Expr(arg.plan, client) if arg.plan is not None else arg.value


def _arg(arg: Arg, context: Any, client: Any) -> Any:
    if arg.plan is None:
        return arg.value
    return evaluate(Expr(arg.plan, client), context, client=client)


# -- bounded fan-out ---------------------------------------------------------


async def fan_out(
    items: list[Any], fn: Callable[[Any], Awaitable[Any]], *, limit: int
) -> list[Any]:
    """Run ``fn`` over ``items`` with at most ``limit`` in flight, results in
    input order. A failing task cancels its siblings and propagates (the first
    failure is raised)."""
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


__all__ = ["evaluate", "truthy", "fan_out"]
