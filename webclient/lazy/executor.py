"""The evaluator: one walk over a ``Plan``, calling the real model methods
by ``getattr`` (Decision: no whitelist, ``_``-names refused at record time).

A plan is walked step by step from its context. Property access and method
calls dispatch by ``getattr``; operator and function steps apply inline.
When the running value is a ``Collection`` and the step is not a
whole-collection op, the rest of the chain runs per element in a bounded
worker pool -- results stream out as they complete and nested collections
recurse the same way. The default error policy around a plan is RETURN (one
bad row must not abort the plan); a step may override it with ``error=``.
"""
from __future__ import annotations

import asyncio
import operator
from typing import Any, AsyncIterator, Awaitable, Callable
from uuid import uuid4

from ..events import Event
from ..document import Collection, Reference, WebBase
from ..core.base import RETURN, default_policy
from .expr import Arg, Expr, Plan, Step

DEFAULT_FANOUT = 8

_OPS = {"eq": operator.eq, "ne": operator.ne, "lt": operator.lt,
        "le": operator.le, "gt": operator.gt, "ge": operator.ge,
        "and": operator.and_, "or": operator.or_, "not": operator.inv}

#: methods whose expression arguments are bound (evaluated per element)
#: inside the method itself, so the evaluator passes them unevaluated.
_BINDS = {"extract", "filter"}


class PlanEvent(Event):
    topic: str = "plan"
    phase: str = "started"           # started / row / done
    detail: dict[str, Any] = {}


# -- entry points ------------------------------------------------------------

async def evaluate(expr: Any, context: Any = None, *, client: Any = None) -> Any:
    """Evaluate ``expr`` against ``context``. A non-Expr value is itself. A
    fanned-out chain yields a ``Collection`` of leaves; a straight chain
    yields its single final value."""
    if not isinstance(expr, Expr):
        return expr
    client = client or expr._client or getattr(context, "_client", None)
    leaves: list[tuple[tuple[int, ...], Any]] = []
    fanned = False
    with default_policy(RETURN):
        async for path, value in _walk(_start(expr._plan, context, client),
                                       expr._plan.steps, 0, context, client):
            fanned = fanned or bool(path)
            leaves.append((path, value))
    if not fanned:
        return leaves[0][1] if leaves else None
    leaves.sort(key=lambda p: p[0])
    values = [v for _, v in leaves]
    if not all(isinstance(v, WebBase) for v in values):
        return values                     # plain leaves (e.g. projected dicts)
    out: Collection[Any] = Collection(root=getattr(context, "name", None))
    out._items = values
    out._client = client
    return out


async def stream(expr: Any, context: Any = None, *,
                 client: Any = None) -> AsyncIterator[Any]:
    """The same walk, yielding leaves as they complete (page order is not
    guaranteed; ``evaluate`` sorts)."""
    client = client or expr._client or getattr(context, "_client", None)
    plan_id = uuid4().hex
    bus = getattr(client, "bus", None)
    if bus is not None:
        bus.publish(PlanEvent(phase="started", plan_id=plan_id))
    rows = 0
    try:
        with default_policy(RETURN):
            async for _, value in _walk(_start(expr._plan, context, client),
                                        expr._plan.steps, 0, context, client):
                # a whole-collection terminal (e.g. project) yields one list;
                # stream its rows individually.
                for row in (value if isinstance(value, list) else [value]):
                    rows += 1
                    if bus is not None:
                        bus.publish(PlanEvent(phase="row", plan_id=plan_id))
                    yield row
    finally:
        if bus is not None:
            bus.publish(PlanEvent(phase="done", plan_id=plan_id,
                                  detail={"rows": rows}))


# -- the walk ---------------------------------------------------------------

def _start(plan: Plan, context: Any, client: Any) -> Any:
    # A document-source plan (remote handle, source={"document_id": ...}) is
    # resolved to its context by the caller (the service looks up the doc);
    # only a Reference source is reconstructed here.
    if plan.source is not None and "document_id" not in plan.source:
        return Reference(**plan.source).bind(client, getattr(context, "_session", None))
    if context is None:
        raise ValueError(
            f"plan rooted at {plan.root or 'a context'} needs a context; pass "
            "one to execute() or root it with Reference(url)")
    return context


def _is_whole(step: Step) -> bool:
    """A step that acts on a Collection itself, not on its elements."""
    return step.kind == "get" and step.name in Collection._WHOLE


async def _walk(value: Any, steps: list[Step], i: int, context: Any,
                client: Any) -> AsyncIterator[tuple[tuple[int, ...], Any]]:
    while i < len(steps):
        step = steps[i]
        if isinstance(value, Collection) and not _is_whole(step):
            rest = steps[i:]
            async for path, leaf in _fan_out(
                    list(value), lambda el: _walk(el, rest, 0, el, client),
                    limit=_limit(client)):
                yield path, leaf
            return
        value, i = await _apply(value, steps, i, context, client)
    yield (), value


async def _apply(value: Any, steps: list[Step], i: int, context: Any,
                 client: Any) -> tuple[Any, int]:
    """Apply the step(s) at ``i``; returns the new value and next index. A
    ``get`` followed by a ``call`` is one method call; a lone ``get`` is a
    property/attribute read."""
    step = steps[i]
    if step.kind == "get":
        nxt = steps[i + 1] if i + 1 < len(steps) else None
        if nxt is not None and nxt.kind == "call":
            args, kwargs = await _call_args(step.name, nxt, context, client)
            result = getattr(value, step.name)(*args, **kwargs)
            return await _settle(result), i + 2
        return getattr(value, step.name), i + 1
    if step.kind == "op":
        if step.name == "not":
            return _OPS["not"](value), i + 1
        other = await _arg(step.args[0], context, client) if step.args else None
        return _OPS[step.name](value, other), i + 1
    if step.kind == "fn":                       # is_empty(x) == x.is_empty()
        return await _settle(getattr(value, step.name)()), i + 1
    if step.kind == "when":                     # when(cond).then(a).otherwise(b)
        cond, then_arg, else_arg = step.args
        chosen = then_arg if _truthy(await _arg(cond, context, client)) else else_arg
        return await _arg(chosen, context, client), i + 1
    raise ValueError(f"cannot evaluate step {step.kind!r}")


async def _call_args(name: str, call: Step, context: Any,
                     client: Any) -> tuple[list[Any], dict[str, Any]]:
    if name in _BINDS:                          # pass sub-plans unevaluated
        args = [_as_expr(a, client) for a in call.args]
        kwargs = {k: _as_expr(v, client) for k, v in call.kwargs.items()}
    else:
        args = [await _arg(a, context, client) for a in call.args]
        kwargs = {k: await _arg(v, context, client) for k, v in call.kwargs.items()}
    return args, kwargs


def _as_expr(arg: Arg, client: Any) -> Any:
    return Expr(arg.plan, client) if arg.plan is not None else arg.value


async def _arg(arg: Arg, context: Any, client: Any) -> Any:
    if arg.plan is None:
        return arg.value
    return await evaluate(Expr(arg.plan, client), context, client=client)


async def _settle(result: Any) -> Any:
    return await result if asyncio.iscoroutine(result) else result


def _truthy(value: Any) -> bool:
    if isinstance(value, WebBase):
        if not value.ok:
            return False
        return bool(value.get()) if hasattr(value, "get") else True
    return bool(value)


# -- bounded fan-out ---------------------------------------------------------

async def fan_out(items: list[Any], fn: Callable[[Any], Awaitable[Any]], *,
                  limit: int) -> list[Any]:
    """Run ``fn`` over ``items``, at most ``limit`` in flight, results in
    order. A failure cancels the siblings (TaskGroup) and propagates."""
    results: list[Any] = [None] * len(items)
    pending = iter(range(len(items)))

    async def worker() -> None:
        for i in pending:
            results[i] = await fn(items[i])

    try:
        async with asyncio.TaskGroup() as group:
            for _ in range(min(limit, len(items)) or 1):
                group.create_task(worker())
    except BaseExceptionGroup as group_exc:
        raise _unwrap(group_exc) from None
    return results


async def _fan_out(items: list[Any],
                   fn: Callable[[Any], AsyncIterator[tuple[tuple[int, ...], Any]]],
                   *, limit: int) -> AsyncIterator[tuple[tuple[int, ...], Any]]:
    """Fan a leaf-yielding walker over elements: a bounded worker pool feeds a
    queue, leaves come out tagged with their index path as they complete."""
    queue: asyncio.Queue[Any] = asyncio.Queue()
    pending = iter(range(len(items)))
    done = object()

    async def worker() -> None:
        for i in pending:
            async for path, leaf in fn(items[i]):
                await queue.put(((i, *path), leaf))

    async def drive() -> None:
        try:
            async with asyncio.TaskGroup() as group:
                for _ in range(min(limit, len(items)) or 1):
                    group.create_task(worker())
        except BaseException as exc:            # surface on the consumer side
            await queue.put(_unwrap(exc))
            raise
        await queue.put(done)

    driver = asyncio.ensure_future(drive())
    try:
        while True:
            item = await queue.get()
            if item is done:
                break
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        if not driver.done():
            driver.cancel()
        try:
            await driver
        except BaseException:
            pass


def _unwrap(exc: BaseException) -> BaseException:
    """A TaskGroup's failure unwrapped: the first row to fail is reported."""
    while isinstance(exc, BaseExceptionGroup):
        exc = exc.exceptions[0]
    return exc


def _limit(client: Any) -> int:
    pool = getattr(client, "pool", None)
    return max(1, pool.max_http) if pool is not None else DEFAULT_FANOUT


__all__ = ["evaluate", "stream", "fan_out", "PlanEvent"]
