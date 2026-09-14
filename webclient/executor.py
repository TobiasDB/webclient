"""The evaluator: one async walk over a recorded ``Plan``.

The core is async (``aevaluate`` / ``astream``); it drives the eager surface by
``getattr`` and ``await``s any op that returns a coroutine (the IO ops --
``resolve`` -- hand back a coroutine when already on the engine loop). A
Collection fans out per element through the bounded ``fan_out``. Sync callers
use ``evaluate`` (a ``loop.run`` bridge); the async client awaits ``aevaluate``
on the engine loop. ``astream`` is *truly* incremental: it evaluates the plan up
to the final fan-out, then runs that fan-out as elements complete (``fan_out_stream``)
and yields each row/element the moment it is ready -- the whole result is never
materialised first. A plan whose tail is not a per-element shape falls back to
evaluate-then-yield.
"""

from __future__ import annotations

import asyncio
import operator
from typing import Any, AsyncIterator, Awaitable, Callable, cast

from .expr import Expr
from .plan import Arg, Step

DEFAULT_FANOUT = 8

#: sentinel: a streamed element dropped by a filter predicate
_DROP = object()

#: element ops that fan out per element (the generated Collection lift). A plan
#: ending in one of these streams its per-element results.
_ELEMENT_OPS = frozenset(
    {
        "attr",
        "text_content",
        "title",
        "render",
        "ref",
        "select",
        "select_all",
        "is_ok",
        "is_empty",
        "message",
        "click",
        "write",
        "reload",
        "screenshot",
        "wait_for",
    }
)

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
    return cast(int, (limits or {}).get("http", DEFAULT_FANOUT))


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

            result = wrap(await cast(Any, result))
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
    """The value a plan starts from: the bound client (WebClient root, whose
    authoring verbs the walk dispatches), a reconstructed Reference (source
    plan), or the passed context (doc/ref/field roots)."""
    if plan.root == "WebClient":
        from .surface import wrap

        if client is None:
            raise ValueError("a WebClient-rooted plan needs a bound client")
        return wrap(client)  # an eager client surface -> dispatches ref/fetch/...
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
    """Yield the plan's rows as they are produced -- truly incremental.

    The plan is evaluated up to the base Collection of the final fan-out; that
    fan-out then runs as-completed (``fan_out_stream``, bounded by the pool) and
    each row/element is yielded the moment its element finishes -- nothing is
    materialised first. Two tail shapes stream: a terminal ``project()`` (rows,
    optionally preceded by ``extract``/``filter``) and a terminal element op
    (e.g. ``.text_content``). Any other plan falls back to evaluate-then-yield.
    """
    from .collection import Collection

    if not isinstance(expr, Expr):
        for row in expr if isinstance(expr, list) else [expr]:
            yield row
        return
    client = client or expr._client or getattr(context, "_client", None)
    if isinstance(context, Expr):  # an Expr context (wc.ref(url)) runs first
        context = await aevaluate(context, client=client)

    steps = expr._plan.steps
    tail = _stream_tail(steps)
    if tail is None:  # not a streamable shape -- evaluate whole, then hand out
        result = await aevaluate(expr, context, client=client)
        for row in result if isinstance(result, list) else [result]:
            yield row
        return

    head, shaping = steps[:tail], steps[tail:]
    base = await _arun(_start(expr._plan, context, client), head, 0, context, client)
    if not isinstance(base, Collection):  # head wasn't a collection -- finish eager
        value = await _arun(base, shaping, 0, context, client)
        for row in value if isinstance(value, list) else [value]:
            yield row
        return
    async for row in _astream_collection(base, shaping, client):
        yield row


def _stream_tail(steps: list[Step]) -> int | None:
    """Index at which a streamable per-element tail begins, or ``None``. A
    terminal ``project()`` (no model) preceded by ``extract``/``filter`` pairs
    streams rows; a terminal element op streams its per-element results."""
    n = len(steps)
    if n < 2 or steps[-2].kind != "get" or steps[-1].kind != "call":
        return None
    name = steps[-2].name
    if name == "project":
        if steps[-1].args:  # project(model): eager only (model not serialisable)
            return None
        i = n - 2
        while (
            i - 2 >= 0
            and steps[i - 2].kind == "get"
            and steps[i - 1].kind == "call"
            and steps[i - 2].name in ("extract", "filter")
        ):
            i -= 2
        return i
    if name in _ELEMENT_OPS:
        return n - 2
    return None


def _parse_shaping(steps: list[Step], client: Any) -> list[tuple[str, Any]]:
    """Parse a run of ``extract``/``filter`` get+call pairs into ops with their
    sub-expressions reconstructed (extract -> {col: Expr}; filter -> [Expr])."""
    ops: list[tuple[str, Any]] = []
    i = 0
    while i + 1 < len(steps):
        get_step, call = steps[i], steps[i + 1]
        if get_step.name == "extract":
            ops.append(
                ("extract", {k: _as_expr(v, client) for k, v in call.kwargs.items()})
            )
        else:  # filter
            ops.append(("filter", [_as_expr(a, client) for a in call.args]))
        i += 2
    return ops


async def _astream_collection(
    base: Any, shaping: list[Step], client: Any
) -> AsyncIterator[Any]:
    """Stream the final fan-out of ``base`` under ``shaping`` as elements
    complete. Rows (``...project()``) or per-element op results are yielded the
    moment each element finishes; a filtered-out element yields nothing."""
    from .collection import Field, _raw, _row_of
    from .errors import RETURN, default_policy

    items = list(base)
    is_project = (
        len(shaping) >= 2
        and shaping[-2].kind == "get"
        and shaping[-2].name == "project"
    )
    if is_project:
        ops = _parse_shaping(shaping[:-2], client)

        async def process(el: Any) -> Any:
            with default_policy(RETURN):  # a missing field is None, not an abort
                for kind, payload in ops:
                    if kind == "extract":
                        row = _row_of(el)
                        if row is not None:
                            for key, sub in payload.items():
                                row[key] = _raw(await aevaluate(sub, el, client=client))
                    else:  # filter: drop the element if any predicate is falsey
                        for pred in payload:
                            if not truthy(await aevaluate(pred, el, client=client)):
                                return _DROP
                shaped = _row_of(el, create=False)
                return shaped if shaped is not None else el

    else:  # a terminal element op: apply it to each element on its own

        async def process(el: Any) -> Any:
            value = await _arun(el, shaping, 0, el, client)
            return value.get() if isinstance(value, Field) else value

    async for result in fan_out_stream(items, process, limit=_fanout_limit(client)):
        if result is not _DROP:
            yield result


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


def _flatten_exceptions(exc: BaseException) -> list[BaseException]:
    """Depth-first leaves of a (possibly nested) ExceptionGroup."""
    if isinstance(exc, BaseExceptionGroup):
        leaves: list[BaseException] = []
        for member in exc.exceptions:
            leaves.extend(_flatten_exceptions(member))
        return leaves
    return [exc]


def _note_siblings(first: BaseException, siblings: list[BaseException]) -> None:
    """Attach the other failures to ``first`` as a PEP 678 note, so a fan-out
    that raises its first failure still surfaces the siblings it cancelled (they
    would otherwise be silently discarded from the traceback)."""
    if siblings:
        detail = "; ".join(f"{type(e).__name__}: {e}" for e in siblings)
        first.add_note(
            f"fan-out: {len(siblings)} sibling task(s) also failed: {detail}"
        )


async def fan_out(
    items: list[Any], fn: Callable[[Any], Awaitable[Any]], *, limit: int
) -> list[Any]:
    """Run ``fn`` over ``items`` with at most ``limit`` in flight, results in
    input order. A failing task cancels its siblings and raises the first
    failure; any sibling failures are surfaced on that exception as a note."""
    results: list[Any] = [None] * len(items)
    pending = iter(range(len(items)))

    async def worker() -> None:
        for i in pending:
            results[i] = await fn(items[i])

    try:
        async with asyncio.TaskGroup() as group:
            for _ in range(min(max(limit, 1), len(items)) or 1):
                group.create_task(worker())
    except BaseExceptionGroup as group_exc:  # raise the first, note the rest
        leaves = _flatten_exceptions(group_exc)
        _note_siblings(leaves[0], leaves[1:])
        raise leaves[0] from None
    return results


async def fan_out_stream(
    items: list[Any], fn: Callable[[Any], Awaitable[Any]], *, limit: int
) -> AsyncIterator[Any]:
    """Run ``fn`` over ``items`` with at most ``limit`` in flight, yielding each
    result the moment it completes (order is completion order, not input order).
    A failing task raises its error and cancels the rest; abandoning the iterator
    (break/GC) cancels every outstanding task in the ``finally``."""
    n = len(items)
    if n == 0:
        return
    sem = asyncio.Semaphore(max(min(limit, n), 1))
    queue: asyncio.Queue[tuple[bool, Any]] = asyncio.Queue()

    async def run(item: Any) -> None:
        async with sem:
            try:
                await queue.put((True, await fn(item)))
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # surfaced on the consuming side
                await queue.put((False, exc))

    tasks = [asyncio.create_task(run(item)) for item in items]
    try:
        for _ in range(n):
            ok, value = await queue.get()
            if not ok:
                # surface any sibling failures already queued before we unwind
                siblings: list[BaseException] = []
                while not queue.empty():
                    ok2, other = queue.get_nowait()
                    if not ok2:
                        siblings.append(other)
                _note_siblings(value, siblings)
                raise value
            yield value
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            try:
                await task
            except BaseException:
                pass


__all__ = [
    "aevaluate",
    "astream",
    "evaluate",
    "truthy",
    "fan_out",
    "fan_out_stream",
]
