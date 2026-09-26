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

import logging
import asyncio
import operator
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, Iterator, TypeVar, cast

from .expr import Expr
from .plan import Arg, Plan, Step

if TYPE_CHECKING:
    from .collection import Collection
    from ..core.document import Document

X = TypeVar("X")  # an item fan_out/fan_out_stream iterates
Y = TypeVar("Y")  # what fn(item) resolves to

DEFAULT_FANOUT = 8

#: the live (browser-page) documents a running plan resolved, collected so the plan
#: releases their page leases when it finishes -- a plan has no handle to
#: ``release(doc)``, so an unreleased browser page would leak its pool lease. The
#: outermost ``aevaluate`` owns the list (a mutable object shared with fan-out child
#: tasks via the copied context); a ``keep_alive`` doc is never collected -- the
#: caller owns it. ``None`` means "not inside a plan run".
_PLAN_LIVE: "ContextVar[list[Document] | None]" = ContextVar("plan_live", default=None)

#: sentinel: a streamed element dropped by a filter predicate
_DROP = object()

#: element ops that fan out per element (the generated Collection lift). A plan
#: ending in one of these streams its per-element results. DERIVED from the
#: Document core's own op tables (call + property ops), so a new Document backing
#: op is streamable automatically -- there is no hand-maintained list to forget
#: (which used to silently degrade streaming to evaluate-then-yield).
_ELEMENT_OPS_CACHE: "frozenset[str] | None" = None


log = logging.getLogger(__name__)


def _element_ops() -> "frozenset[str]":
    """The set of Document ops (call + property) that fan out per element, read once from the
    Document core's own op tables and cached -- so a new backing op streams without a manual list."""
    global _ELEMENT_OPS_CACHE
    if _ELEMENT_OPS_CACHE is None:
        from ..core.document import Document

        _ELEMENT_OPS_CACHE = frozenset(Document.ops()) | frozenset(Document.prop_ops())
    return _ELEMENT_OPS_CACHE

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
#: element/page inside the op); the executor calls their async ``a<name>`` form.
#: ``paginate`` is bound so its ``stop``/``key`` predicates evaluate per page.
_BINDS = {"extract", "filter", "paginate"}
#: value ops (Field methods) that also apply to a plain read -- a str / number / a list of them.
_VALUE_OPS = frozenset({"number", "map", "date", "datetime", "split"})
#: ops acting on a Collection as a whole (everything else fans out per element)
_COLL_OPS = {"extract", "filter", "project", "limit", "documents", "merge"}


def _iscoro(value: Any) -> bool:
    """Whether ``value`` is a coroutine (an IO op's result to await, vs an in-memory value)."""
    return asyncio.iscoroutine(value)


def _leases_pages(steps: "list[Step] | None") -> bool:
    """Whether running ``steps`` per fan-out element leases a browser page -- i.e.
    the branch contains a ``resolve``/``fetch`` that goes straight to a browser
    (``browser=True`` / ``"always"``). ``"auto"`` is not counted: it usually stays
    static, and if it does escalate the per-element release + page semaphore still
    bound it -- so a browser fan-out is capped without slowing the common static one."""
    for i, s in enumerate(steps or ()):
        if s.kind == "get" and s.name in ("resolve", "fetch"):
            nxt = steps[i + 1] if steps and i + 1 < len(steps) else None
            if nxt is not None and nxt.kind == "call":
                arg = nxt.kwargs.get("browser")
                if arg is not None and arg.value in (True, "always"):
                    return True
    return False


def _fanout_limit(client: Any, steps: "list[Step] | None" = None) -> int:
    """The width of a fan-out: the http concurrency by default, but bounded by the
    PAGE pool when the branches each lease a browser page -- otherwise the fan-out
    schedules ``http`` (10) branches that queue behind the smaller page semaphore
    (over-subscription: harmless since the per-element release drains them, but it
    inflates ``waiting`` and holds idle tasks). ``steps`` is the per-element plan."""
    pool = getattr(client, "_pool", None)
    limits = getattr(pool, "_limits", None) if pool is not None else None
    limits = limits or {}
    http = cast(int, limits.get("http", DEFAULT_FANOUT))
    if _leases_pages(steps):
        return min(http, cast(int, limits.get("page", http)))
    return http


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


async def _release_pages(live: "list[Document]") -> None:
    """Return each plan-owned browser page in ``live`` to the pool. ``_arelease`` is
    idempotent (a page already released -- e.g. an auto-escalated one -- is a no-op),
    so this is safe to call for every collected page."""
    for doc in live:
        client_ = getattr(doc, "_client", None)
        if client_ is not None:
            try:
                await client_._arelease(doc)
            except Exception:
                pass


@asynccontextmanager
async def _plan_scope() -> "AsyncIterator[None]":
    """Scope a plan run's browser-page collection: the outermost entry (``aevaluate``
    / ``astream``) owns a list into which every browser page a step resolves (without
    ``keep_alive``) is gathered, and releases them all when the plan finishes -- a
    plan has no ``release(doc)`` handle, so a live page would otherwise leak its pool
    lease. The list is shared with fan-out child tasks via the copied context."""
    outer = _PLAN_LIVE.get() is None
    token = _PLAN_LIVE.set([]) if outer else None
    try:
        yield
    finally:
        if outer and token is not None:
            live = _PLAN_LIVE.get() or []
            _PLAN_LIVE.reset(token)
            await _release_pages(live)


def _per_element(
    fn: "Callable[[Any], Awaitable[Any]]",
) -> "Callable[[Any], Awaitable[Any]]":
    """Wrap a fan-out unit of work so the browser pages IT resolves (without
    ``keep_alive``) are released the moment that element finishes -- not deferred to
    the end of the whole plan. Without this, every page a fan-out resolves is held
    until the plan completes, so a plan (or stream) that resolves more pages than the
    page-pool cap deadlocks: the cap-th lease is taken, the next blocks, and nothing
    is released until the fan-out (which is waiting on that lease) returns. A released
    page keeps its captured content, so downstream in-memory ops are unaffected (this
    is exactly what the plan-end release already did, only sooner)."""

    async def wrapped(el: Any) -> Any:
        parent = _PLAN_LIVE.get()
        if parent is None:  # not inside a plan scope: nothing to bound
            return await fn(el)
        token = _PLAN_LIVE.set([])
        try:
            return await fn(el)
        finally:
            mine = _PLAN_LIVE.get() or []
            _PLAN_LIVE.reset(token)
            await _release_pages(mine)

    return wrapped


async def aevaluate(expr: Any, context: Any = None, *, client: Any = None) -> Any:
    """Evaluate ``expr`` against ``context`` (async). A non-Expr value is itself.
    The outermost call releases any browser page a plan step resolves (see
    :func:`_plan_scope`)."""
    if not isinstance(expr, Expr):
        return expr
    async with _plan_scope():
        client = client or expr._client or getattr(context, "_client", None)
        if isinstance(context, Expr):  # an Expr context (wc.ref(url)) runs first
            with _plan_at(()):
                context = await aevaluate(context, client=client)
        if log.isEnabledFor(logging.DEBUG):
            log.debug("evaluate %s", expr._plan.describe())
        with _plan_at(_nested_address()), _plan_id(expr._plan):
            value = _start(expr._plan, context, client)
            return await _arun(value, expr._plan.steps, 0, context, client)


# -- step addresses ------------------------------------------------------------
#: where the plan being walked sits: () at the root, else the enclosing step's address plus the arg
#: segment the sub-plan came from (``("6", "kw:title")``). Each step runs at this + its own index.
_PLAN_AT: ContextVar[tuple[str, ...]] = ContextVar("webclient_plan_at", default=())


def _nested_address() -> tuple[str, ...]:
    """The address a (sub-)plan starting NOW sits at: the current step's, with the segment its caller
    pushed (``kw:title``) -- or ``sub`` when the caller named none (a bound op's own sub-plan)."""
    from ..events import CURRENT_STEP

    cur = CURRENT_STEP.get()
    return (*cur, "sub") if cur and cur[-1].isdigit() else cur


@contextmanager
def _plan_id(plan: "Plan") -> Iterator[None]:
    """The plan a TOP-LEVEL evaluation runs (a sub-plan's events belong to the plan it is part of)."""
    from ..events import CURRENT_PLAN

    if CURRENT_PLAN.get() is not None:
        yield
        return
    token = CURRENT_PLAN.set(plan.id)
    try:
        yield
    finally:
        CURRENT_PLAN.reset(token)


@contextmanager
def _plan_at(at: tuple[str, ...]) -> Iterator[None]:
    token = _PLAN_AT.set(at)
    try:
        yield
    finally:
        _PLAN_AT.reset(token)


@contextmanager
def _step_at(index: int) -> Iterator[None]:
    """Run as the step at ``index`` of the plan being walked (the bus stamps it on every event)."""
    from ..events import CURRENT_STEP

    token = CURRENT_STEP.set((*_PLAN_AT.get(), str(index)))
    try:
        yield
    finally:
        CURRENT_STEP.reset(token)


@contextmanager
def arg_segment(segment: "str | None") -> Iterator[None]:
    """Descend into the arg ``segment`` (``kw:title``, ``arg:0``) of the running step: a sub-plan
    evaluated inside is addressed under it."""
    from ..events import CURRENT_STEP

    if not segment:
        yield
        return
    token = CURRENT_STEP.set((*CURRENT_STEP.get(), *segment.split("/")))
    try:
        yield
    finally:
        CURRENT_STEP.reset(token)


def _first_coll_op(steps: list[Step]) -> int:
    """The index of the first whole-collection op (``extract``/``filter``/``project``/… -- see
    ``_COLL_OPS``) in ``steps``, or ``len(steps)`` if there is none. The boundary that splits a
    per-element PREFIX (element ops fanned out per element) from a collection SUFFIX (shaping run
    once on the merged collection)."""
    for j, s in enumerate(steps):
        if s.kind == "get" and s.name in _COLL_OPS:
            return j
    return len(steps)


def _merge_fanout(results: list[Any], root: str, client: Any) -> Any:
    """Combine a per-element fan-out's results into the value the chain continues on. All single
    cores -> a ``Collection`` of them (a per-element ``select``/``resolve``). All ``Collection``s
    -> ONE flat ``Collection`` concatenating them in input order (a per-element ``select_all`` /
    ``links`` -- the flat-map that keeps a multi-document plan flat, not nested). Otherwise (scalar
    results, e.g. ``attr('text')`` per element) -> the plain list."""
    from .collection import Collection
    from ..core.web_core import WebCore

    if results and all(isinstance(r, WebCore) for r in results):
        return Collection(results, client=client, root=root)
    if results and all(isinstance(r, Collection) for r in results):
        merged = [item for coll in results for item in coll]  # flat-map: concat, input order
        return Collection(merged, client=client, root=root)
    return results


async def _arun(
    value: Any, steps: list[Step], i: int, context: Any, client: Any, base: int = 0
) -> Any:
    """Walk the remaining plan steps over a running value. When the value is a Collection and the
    next step is an ELEMENT op, fan out only the element-op PREFIX per element (up to the next
    whole-collection op), FLAT-MAP the results into one Collection (so a per-element ``select_all``
    yields a flat record Collection, not a nested list of Collections), then run the collection
    SUFFIX (``extract``/``filter``/``project``/``limit``) once on the merged Collection. Otherwise
    apply steps one at a time. Returns the materialised result."""
    from .collection import Collection

    while i < len(steps):
        step = steps[i]
        if isinstance(value, Collection) and not (
            step.kind == "get" and step.name in _COLL_OPS
        ):
            rest = steps[i:]
            split = _first_coll_op(rest)  # prefix = element ops; suffix = the collection shaping
            prefix = rest[:split]
            limit = _fanout_limit(client, prefix)
            at = base + i  # the prefix's steps keep their addresses in the full plan
            with _step_at(at):
                _note_parallel(len(value), limit, prefix, client)
            results = await fan_out(
                list(value),
                _per_element(lambda el: _arun(el, prefix, 0, el, client, at)),
                limit=limit, bus=getattr(client, "bus", None),
            )
            merged = _merge_fanout(results, value.root, client)
            if split < len(rest) and isinstance(merged, Collection):
                # continue the whole-collection shaping (extract/filter/project/...) on the FLAT
                # merged collection, so the rows are flat across every fanned-out element.
                return await _arun(merged, rest[split:], 0, merged, client, at + split)
            return merged
        with _step_at(base + i):
            t0 = time.perf_counter()
            try:
                out, nxt = await _aapply(value, steps, i, context, client)
            except BaseException as exc:
                _note_result(steps[i], None, t0, client, exc)
                raise
            _note_result(steps[i], out, t0, client)
        value, i = out, nxt
    return value


async def _aapply(
    value: Any, steps: list[Step], i: int, context: Any, client: Any
) -> tuple[Any, int]:
    """Apply one step to the running value and return ``(new_value, next_index)``: a ``get`` reads
    an attribute or (fused with a following ``call``) invokes a method; ``op``/``when``/``fn``
    evaluate their operands and produce the value. Advances the index past what it consumed."""
    step = steps[i]
    if step.kind == "get":
        nxt = steps[i + 1] if i + 1 < len(steps) else None
        if nxt is not None and nxt.kind == "call":
            return await _acall(value, step.name, nxt, context, client), i + 2
        return getattr(value, step.name), i + 1
    if step.kind == "op":
        if step.name == "not":  # unary: ~expr -> logical not (no rhs arg)
            return (not truthy(value)), i + 1
        other = await _aarg(step.args[0], context, client, "arg:0") if step.args else None
        return _OPS[step.name](value, other), i + 1
    if step.kind == "when":
        cond, then_arg, else_arg = step.args
        chosen, seg = (then_arg, "arg:1") if truthy(await _aarg(cond, context, client, "arg:0")) else (else_arg, "arg:2")
        return await _aarg(chosen, context, client, seg), i + 1
    if step.kind == "fn":  # is_empty(x) == x.is_empty()
        op = getattr(value, step.name, None)
        result = op() if callable(op) else op
        if _iscoro(result):
            from ..interface import wrap

            result = wrap(await cast(Any, result))
        return result, i + 1
    raise ValueError(f"cannot evaluate step {step.kind!r}")


async def _acall(value: Any, name: str, call: Step, context: Any, client: Any) -> Any:
    """Invoke method ``name`` on the running value with its call step's args. Handles the special
    ops (``step`` replays an action sub-plan against the held page; ``field``/``reference`` read an
    extracted column; the bound ops get their sub-plans unevaluated), and awaits + wraps an IO op's
    result, registering any browser page it opened for release."""
    # step(action): a SEQUENCE step against ONE held live page. Replay the action
    # sub-plan (a wait_for/click/write/... chain) against the CURRENT doc -- the
    # same held page the resolve opened -- as a side effect, then hand the doc back
    # so later .step(...)/.extract(...) chain onto it (extract accumulates the row).
    # Run WITHIN the current plan scope (not a fresh one) so every page the sequence
    # opens -- the root AND any sub-resolve inside a step -- is owned by the sequence
    # and released together when it finishes (see ``_plan_scope``; a nested
    # ``aevaluate`` shares the outer live registry and releases nothing itself).
    if name == "step":
        if call.args and call.args[0].plan is not None:
            action = Expr(call.args[0].plan, client)
            with arg_segment("arg:0"):
                await aevaluate(action, value, client=client)
        if call.fp:  # a recorded step carries a state fingerprint -- compare, never gate
            await _check_divergence(value, call.fp, client)
        return value
    # field(k) / reference(k) read an extracted column off the element's _row
    if name in ("field", "reference"):
        from .collection import _row_of

        row = _row_of(value, create=False)
        if row is not None:
            column = row.get(await _aarg(call.args[0], context, client))
            if name == "field":
                from .collection import Field

                return column if isinstance(column, Field) else Field(column)
            return column
    if name == "alias":  # the column's NAME rides on the chain; extract reads it (see columns_of)
        return value
    # a plain read (str / number / list): the Field ops apply (a str's own .split is not the op)
    if name in _VALUE_OPS and (not hasattr(value, name) or isinstance(value, str)):
        from .collection import Collection, Field

        vargs = [await _aarg(a, context, client, f"arg:{n}") for n, a in enumerate(call.args)]
        vkw = {k: await _aarg(v, context, client, f"kw:{k}") for k, v in call.kwargs.items()}

        def one(v: Any) -> Any:
            return getattr(v if isinstance(v, Field) else Field(v), name)(*vargs, **vkw)

        if isinstance(value, (list, Collection)):
            return [one(v) for v in value]
        return one(value)
    if name in _BINDS:  # sub-plans passed unevaluated to the async bound op
        args = [_as_expr(a, client) for a in call.args]
        kwargs = {k: _as_expr(v, client) for k, v in call.kwargs.items()}
        bound = await getattr(value, "a" + name)(*args, **kwargs)
        if name == "paginate":
            _note_fanout(value, name, None, bound, client)
        return bound
    args = [await _aarg(a, context, client, f"arg:{n}") for n, a in enumerate(call.args)]
    kwargs = {k: await _aarg(v, context, client, f"kw:{k}") for k, v in call.kwargs.items()}
    _note_step(value, name, args, client)
    result = getattr(value, name)(*args, **kwargs)
    if name in _FANOUT_OPS and not _iscoro(result):
        _note_fanout(value, name, next((a for a in args if isinstance(a, str)), None), result, client)
    if _iscoro(result):  # an IO op (resolve): await, then wrap the core it yields
        from ..interface import wrap

        core = await result
        reg = _PLAN_LIVE.get()  # collect a plan-resolved browser page for release
        if (
            reg is not None
            and getattr(core, "_page", None) is not None
            and not getattr(core, "_keep_alive", False)
        ):
            reg.append(core)
        return wrap(core)
    return result


#: the plan ops a watcher wants to SEE happen on the page (a Player highlights the target
#: and moves its pointer there): the element ops and the interactions.
_STEP_OPS = frozenset({"select", "select_all", "attr", "text_content", "extract", "click", "write",
                       "scroll", "goto", "wait_for", "paginate", "resolve"})


#: ops that FAN OUT (one value becomes many): their match count is published as a fanout event
_FANOUT_OPS = frozenset({"select_all", "links"})


def _note_parallel(n: int, limit: int, steps: "list[Step] | None", client: Any) -> None:
    """Publish ``PlanEvent(phase="parallel")`` as a fan-out starts: how many items, how many run
    at once (``limit``: the http concurrency, or the page pool when each item leases a page) and
    which resource bounds it -- so a run view can show what runs in parallel."""
    bus = getattr(client, "bus", None) if client is not None else None
    if bus is None:
        return
    from ..models import PlanEvent

    bus.publish(PlanEvent(phase="parallel", detail={
        "n": n, "limit": max(min(limit, n), 1) if n else 0,
        "bound": "page" if _leases_pages(steps) else "http",
        "ops": [s.name for s in (steps or ()) if s.kind == "get"][:8],
    }))


def _note_fanout(value: Any, name: str, selector: "str | None", result: Any, client: Any) -> None:
    """Publish ``PlanEvent(phase="fanout")`` -- how many items an op fanned out to (the matches of a
    select_all, the pages of a paginate) -- so a run view can show a stage's progress against it."""
    bus = getattr(client, "bus", None) if client is not None else None
    if bus is None:
        return
    try:
        n = len(result)
    except TypeError:
        return
    from ..models import PlanEvent

    bus.publish(PlanEvent(phase="fanout", document_id=getattr(value, "name", None) or None,
                          detail={"op": name, "selector": selector, "n": n}))


def _describe_value(value: Any) -> "dict[str, Any]":
    """What a step produced, for a run view: its KIND (Reference / Document / Element -- a document
    that is a part of a page -- / Collection / Field / list / value), the page it is or is on, how many
    it holds, a short preview of a value, and whether it is ok (a RETURN-policy miss is not)."""
    from .collection import Collection, Field

    out: "dict[str, Any]" = {}
    cls = type(value).__name__
    if isinstance(value, Collection):
        out.update(kind="Collection", n=len(value), of=_member_kind(list(value)[:1]), document_id=None, parent=getattr(value, "root", None) or None)
    elif isinstance(value, Field):
        raw = value.get()
        out.update(kind="Field", preview=_preview(raw))
    elif cls in ("Document", "Reference") or hasattr(value, "_page") or hasattr(value, "url"):
        name = getattr(value, "name", "") or ""
        kind = cls if cls in ("Document", "Reference") else "Document"
        if kind == "Document" and not name:
            kind = "Element"
        out.update(kind=kind, document_id=name or None, parent=getattr(value, "root", None) or None)
        url = getattr(value, "final_url", None) or getattr(value, "url", None)
        if url:
            out["url"] = str(url)
    elif isinstance(value, dict):
        out.update(kind="Row", preview=_preview(value))
    elif isinstance(value, (list, tuple)) and value and all(hasattr(v, "_client") and hasattr(v, "root") for v in value):
        # a list of cores (an eager select_all's matches, before they are wrapped): a Collection
        first = value[0]
        out.update(kind="Collection", n=len(value), of=_member_kind([first]), document_id=None, parent=getattr(first, "root", None) or getattr(first, "name", None) or None)
    elif isinstance(value, (list, tuple)):
        out.update(kind="list", n=len(value), preview=_preview(list(value)[:5]))
    else:
        out.update(kind="value", preview=_preview(value))
    ok = getattr(value, "ok", True)
    if ok is False:
        err = getattr(value, "error", None)
        out.update(ok=False, error=getattr(err, "code", None) or "miss")
    return out


def _member_kind(members: "list[Any]") -> "str | None":
    """What a collection holds: ``Element`` (a select_all's matches -- parts of a page), ``Document`` (pages:
    a paginate's), ``Reference`` (links), or a row / value."""
    if not members:
        return None
    m = members[0]
    cls = type(m).__name__
    if cls == "Reference" or (hasattr(m, "url") and not hasattr(m, "content") and hasattr(m, "method")):
        return "Reference"
    if hasattr(m, "_client") and hasattr(m, "root"):
        return "Document" if getattr(m, "name", "") else "Element"
    return "Row" if isinstance(m, dict) else "Value"


def _preview(v: Any) -> Any:
    """A short, serialisable preview of a value (strings clipped; containers summarised)."""
    if v is None or isinstance(v, (bool, int, float)):
        return v
    if isinstance(v, str):
        return v if len(v) <= 160 else v[:157] + "..."
    if isinstance(v, dict):
        return {str(k): _preview(x) for k, x in list(v.items())[:12]}
    if isinstance(v, (list, tuple)):
        return [_preview(x) for x in list(v)[:5]]
    return _preview(str(v))


def _note_result(step: Step, value: Any, t0: float, client: Any, exc: "BaseException | None" = None) -> None:
    """Publish ``PlanEvent(phase="result")`` as a step finishes: the op, what it produced (see
    ``_describe_value``), how long it took, or the error it raised. With the bus's step address and
    item path this places the step's OUTPUT in the plan -- the page it fetched, the N it fanned out to,
    the value it read -- so a run view is built from the plan and these, not guessed."""
    if step.kind != "get" or step.name in ("alias",):
        return
    bus = getattr(client, "bus", None) if client is not None else None
    if bus is None:
        return
    from ..models import PlanEvent

    detail: "dict[str, Any]" = {"op": step.name, "ms": round((time.perf_counter() - t0) * 1000, 2)}
    if exc is not None:
        err = getattr(exc, "error", None)
        detail.update(ok=False, error=getattr(err, "code", None) or type(exc).__name__,
                      message=str(getattr(err, "detail", None) or exc)[:300])
        if isinstance(exc, asyncio.CancelledError):
            detail["error"] = "cancelled"
    else:
        try:
            detail.update(_describe_value(value))
        except Exception:  # noqa: BLE001 - describing is best-effort; the run goes on
            pass
    detail.setdefault("ok", True)
    # an eager project's rows, one per item (a streamed project publishes each as its item finishes): which item
    # each row came from -- in order, the rows ARE the items (a filter drops before the fan-out's indices are given)
    if exc is None and step.name == "project" and isinstance(value, list) and value and all(isinstance(r, dict) for r in value):
        from ..events import CURRENT_ITEM

        for k, row in enumerate(value):
            token = CURRENT_ITEM.set((*CURRENT_ITEM.get(), k))
            try:
                bus.publish(PlanEvent(phase="result", detail={"op": "project", "kind": "Row", "preview": _preview(row), "ok": True, "ms": 0}))
            finally:
                CURRENT_ITEM.reset(token)
    doc = detail.get("document_id") or detail.get("parent")
    bus.publish(PlanEvent(phase="result", document_id=doc if isinstance(doc, str) and doc.startswith("doc:") else None, detail=detail))


def _note_step(value: Any, name: str, args: "list[Any]", client: Any) -> None:
    """Publish ``PlanEvent(phase="step")`` for an element / interaction op of a running plan:
    the op, its selector (the first string arg) and the document it runs on -- the trail a
    replay follows (select -> highlight + move there, attr -> highlight, click -> move + click)."""
    if name not in _STEP_OPS:
        return
    bus = getattr(client, "bus", None) if client is not None else None
    if bus is None:
        return
    from ..models import PlanEvent

    selector = next((a for a in args if isinstance(a, str)), None)
    bus.publish(PlanEvent(
        phase="step", document_id=getattr(value, "name", None) or None,
        detail={"op": name, "selector": selector, "args": [a for a in args if isinstance(a, (str, int, float, bool))][:4]},
    ))


async def _check_divergence(doc: Any, recorded: str, client: Any) -> None:
    """Advisory sequence-divergence check on replay: compare the live state's fingerprint
    to the one recorded for this step. A mismatch is surfaced through the event bus (a
    ``PlanEvent(phase="divergence")``) and a logged warning -- it NEVER raises or blocks
    the run (fingerprints are debug/drift signals, not a gate)."""
    from ..core.document.fingerprint import fingerprint

    live = await fingerprint(doc)
    if not live or live == recorded:
        return  # unreadable, or the reached state matches what was recorded
    import logging

    logging.getLogger("webclient").warning(
        "sequence divergence on replay: recorded page state %s, live %s (advisory)",
        recorded, live,
    )
    bus = getattr(client, "bus", None)
    if bus is not None:
        from ..models import PlanEvent

        bus.publish(PlanEvent(
            phase="divergence",
            detail={"recorded": recorded, "live": live, "document_id": getattr(doc, "id", None)},
        ))


def _as_expr(arg: Arg, client: Any) -> Any:
    """A plan arg as an UNEVALUATED ``Expr`` (for the bound ops that evaluate per element),
    or its literal value -- never runs the sub-plan."""
    return Expr(arg.plan, client) if arg.plan is not None else arg.value


async def _aarg(arg: Arg, context: Any, client: Any, segment: "str | None" = None) -> Any:
    """A plan arg EVALUATED to a concrete value: a literal as-is, a sub-plan run against the
    current context (addressed under ``segment`` -- ``arg:0`` / ``kw:name`` -- of the running step)."""
    if arg.plan is None:
        return arg.value
    with arg_segment(segment):
        return await aevaluate(Expr(arg.plan, client), context, client=client)


def _start(plan: "Plan", context: Any, client: Any) -> Any:
    """The value a plan starts from: the bound client (WebClient root, whose
    authoring verbs the walk dispatches), a reconstructed Reference (source
    plan), or the passed context (doc/ref/field roots)."""
    if plan.root == "WebClient":
        from ..interface import wrap

        if client is None:
            raise ValueError("a WebClient-rooted plan needs a bound client")
        return wrap(client)  # an eager client surface -> dispatches ref/fetch/...
    if plan.source is not None and "document_id" not in plan.source:
        from ..core.reference import Reference
        from ..core.session import Session
        from ..interface import wrap

        core = Reference(**plan.source)
        if isinstance(context, Session):  # a session-bound reference
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
    optionally preceded by ``extract``/``filter``) and a terminal element CALL op
    (e.g. ``.transport()``/``.attr(...)``). Any other plan falls back to
    evaluate-then-yield.
    """
    from .collection import Collection

    if not isinstance(expr, Expr):
        for row in expr if isinstance(expr, list) else [expr]:
            yield row
        return
    async with _plan_scope():  # release any browser page the stream resolves
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
        with _plan_at(_nested_address()), _plan_id(expr._plan):
            base = await _arun(_start(expr._plan, context, client), head, 0, context, client)
            if not isinstance(base, Collection):  # head wasn't a collection -- finish eager
                value = await _arun(base, shaping, 0, context, client, tail)
                for row in value if isinstance(value, list) else [value]:
                    yield row
                return
            async for row in _astream_collection(base, shaping, client, tail):
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
    if name in _element_ops():
        return n - 2
    return None


def _parse_shaping(steps: list[Step], client: Any) -> list[tuple[str, Any, int]]:
    """Parse a run of ``extract``/``filter`` get+call pairs into ops with their
    sub-expressions reconstructed (extract -> {col: Expr}; filter -> [Expr]) and their index."""
    ops: list[tuple[str, Any, int]] = []
    i = 0
    while i + 1 < len(steps):
        get_step, call = steps[i], steps[i + 1]
        if get_step.name == "extract":
            from .collection import columns_of

            ops.append(
                ("extract", columns_of([_as_expr(a, client) for a in call.args],
                                       {k: _as_expr(v, client) for k, v in call.kwargs.items()}), i)
            )
        else:  # filter
            ops.append(("filter", [_as_expr(a, client) for a in call.args], i))
        i += 2
    return ops


async def _astream_collection(
    base: "Collection[Any]", shaping: list[Step], client: Any, at: int = 0
) -> AsyncIterator[Any]:
    """Stream the final fan-out of ``base`` under ``shaping`` as elements
    complete. Rows (``...project()``) or per-element op results are yielded the
    moment each element finishes; a filtered-out element yields nothing."""
    from .collection import (
        Field,
        _project_row,
        _row_of,
        apply_extract,
        flatten_row,
        survives_filters,
    )

    items = list(base)
    is_project = (
        len(shaping) >= 2
        and shaping[-2].kind == "get"
        and shaping[-2].name == "project"
    )
    if is_project:
        ops = _parse_shaping(shaping[:-2], client)
        pkw = {k: v.value for k, v in shaping[-1].kwargs.items()}  # project(flatten=…, sep=…)

        async def process(el: Any) -> Any:
            # the SAME shaping primitives the eager Collection uses, so a streamed
            # row and a collected row of the same plan are identical (incl. the
            # Reference->URL / Field->value row cleaning).
            for kind, payload, idx in ops:
                with _step_at(at + idx):
                    t0 = time.perf_counter()
                    if kind == "extract":
                        await apply_extract(el, payload, client)
                        _note_result(shaping[idx], el, t0, client)
                    elif not await survives_filters(el, payload, client):
                        return _DROP
            shaped = _row_of(el, create=False)
            row = flatten_row(_project_row(shaped), pkw.get("flatten"), pkw.get("sep", ".")) if shaped is not None else el
            with _step_at(at + len(shaping) - 2):  # the project step, for THIS item: the row it projected
                _note_result(shaping[-2], row, time.perf_counter(), client)
            return row

    else:  # a terminal element op: apply it to each element on its own

        async def process(el: Any) -> Any:
            value = await _arun(el, shaping, 0, el, client, at)
            return value.get() if isinstance(value, Field) else value

    limit = _fanout_limit(client, shaping)
    with _step_at(at):
        _note_parallel(len(items), limit, shaping, client)
    async for result in fan_out_stream(items, _per_element(process), limit=limit, bus=getattr(client, "bus", None)):
        if result is not _DROP:
            yield result


# -- sync bridge -------------------------------------------------------------


def evaluate(expr: Any, context: Any = None, *, client: Any = None) -> Any:
    """Sync entry: run ``aevaluate`` on the client's engine loop."""
    if not isinstance(expr, Expr):
        return expr
    client = client or expr._client or getattr(context, "_client", None)
    from ..core.client import default_client

    engine = client if client is not None else default_client()
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


async def _as_item(i: int, fn: Callable[[X], Awaitable[Y]], item: X, bus: Any) -> Y:
    """Run one fan-out item with its index path set (so every event it publishes carries it),
    publishing ``PlanEvent(phase="item")`` when it ends: ``ok``, ``dropped`` (filtered out) or
    ``failed`` (with the error code)."""
    from ..events import CURRENT_ITEM

    token = CURRENT_ITEM.set((*CURRENT_ITEM.get(), i))
    try:
        try:
            out = await fn(item)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if bus is not None:
                from ..models import PlanEvent

                err = getattr(exc, "error", None)
                bus.publish(PlanEvent(phase="item", detail={"status": "failed", "code": getattr(err, "code", type(exc).__name__)}))
            raise
        if bus is not None:
            from ..models import PlanEvent

            bus.publish(PlanEvent(phase="item", detail={"status": "dropped" if out is _DROP else "ok"}))
        return out
    finally:
        CURRENT_ITEM.reset(token)


async def fan_out(
    items: list[X], fn: Callable[[X], Awaitable[Y]], *, limit: int, bus: Any = None
) -> list[Y]:
    """Run ``fn`` over ``items`` with at most ``limit`` in flight, results in
    input order. A failing task cancels its siblings and raises the first
    failure; any sibling failures are surfaced on that exception as a note.
    Each item runs with its index on the item path (events carry it); ``bus``
    also gets a ``plan.item`` event as each one ends."""
    results: list[Any] = [None] * len(items)
    pending = iter(range(len(items)))

    async def worker() -> None:
        for i in pending:
            results[i] = await _as_item(i, fn, items[i], bus)

    try:
        async with asyncio.TaskGroup() as group:
            for _ in range(min(max(limit, 1), len(items)) or 1):
                group.create_task(worker())
    except BaseExceptionGroup as group_exc:  # raise the first, note the rest
        leaves = _flatten_exceptions(group_exc)
        _note_siblings(leaves[0], leaves[1:])
        raise leaves[0] from None
    return cast("list[Y]", results)


async def fan_out_stream(
    items: list[X], fn: Callable[[X], Awaitable[Y]], *, limit: int, bus: Any = None
) -> AsyncIterator[Y]:
    """Run ``fn`` over ``items`` with at most ``limit`` in flight, yielding each
    result the moment it completes (order is completion order, not input order).
    A failing task raises its error and cancels the rest; abandoning the iterator
    (break/GC) cancels every outstanding task in the ``finally``."""
    n = len(items)
    if n == 0:
        return
    sem = asyncio.Semaphore(max(min(limit, n), 1))
    queue: asyncio.Queue[tuple[bool, Any]] = asyncio.Queue()

    async def run(i: int, item: X) -> None:
        async with sem:
            try:
                await queue.put((True, await _as_item(i, fn, item, bus)))
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # surfaced on the consuming side
                await queue.put((False, exc))

    tasks = [asyncio.create_task(run(i, item)) for i, item in enumerate(items)]
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
