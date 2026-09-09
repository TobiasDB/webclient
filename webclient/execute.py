"""The evaluator: one implementation, both modes.

Eager combinators build a plan and run it here immediately; lazy chains build
the same plan and run it here on `.collect()`. Ops are invoked through the
registry — the very same implementations the direct sync surface calls — so
there is no second `select` and no second `attr` anywhere.

Two rules the old engine broke, enforced here:

* every step kind is dispatched explicitly; an unhandled one raises
  `PlanError` rather than passing the value through untouched (which is how a
  second `.map()` used to vanish);
* fan-out is chunked, so a listing of ten thousand elements never becomes ten
  thousand simultaneous tasks, and a failing row cannot orphan its siblings.
"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .errors import PlanError, WebClientError
from .ops import REGISTRY, _check_capability, _spec_for
from .plan import (Arg, BinOpStep, CallStep, ExplodeStep, FilterStep,
                   LimitStep, MapStep, OtherwiseStep, Plan, Projection, Source,
                   ThenStep)
from .records import Err, Record, RecordSet
from .values import (Failure, Selection, Value, _EAGER_OPS, _unwrap)

DEFAULT_FANOUT = 8


class _Dropped(Exception):
    """A row removed by `otherwise(DROP_ROW)`."""


@dataclass
class Outcome:
    value: Any = None
    failure: Failure | None = None
    dropped: bool = False
    at: Any = None                 # the value the failing step was applied to

    @property
    def ok(self) -> bool:
        return self.failure is None and not self.dropped


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #

def evaluate_now(plan: Plan, receiver: Any) -> Any:
    """Run a plan built by an eager combinator against its receiver."""
    from .engine import run_sync
    client = getattr(receiver, "_client", None)
    outcome = run_sync(_execute(plan, receiver, client, None))
    result = _finish(outcome)
    recorder = getattr(receiver, "_record_extracted", None)
    if recorder is not None and isinstance(result, Record) and result.ok:
        recorder(result)
    return result


def collect_plan(plan: Plan, context: Any = None, *,
                 client: Any = None) -> Any:
    """Run a recorded plan. A rooted plan needs no context."""
    from .engine import run_sync
    if context is None and plan.source.kind == "context":
        raise PlanError(
            "this plan is rooted at a context — pass one to collect(), or "
            'root it with ref("…") / doc("…")')
    if client is None:
        client = getattr(context, "_client", None) or getattr(
            context, "bound", None)
    return _finish(run_sync(_execute(plan, context, client, None)))


async def acollect_plan(plan: Plan, context: Any = None, *,
                        client: Any = None) -> Any:
    """The same, for callers already inside the event loop."""
    return _finish(await _execute(plan, context, client, None))


def _finish(outcome: Outcome) -> Any:
    if outcome.dropped:
        return None
    if outcome.failure is not None:
        error = outcome.failure.error
        if error is not None:
            raise error
        raise WebClientError(outcome.failure.message)
    return outcome.value


# --------------------------------------------------------------------------- #
# Plan walking
# --------------------------------------------------------------------------- #

async def _execute(plan: Plan, context: Any, client: Any,
                   row: Mapping[str, Any] | None) -> Outcome:
    try:
        start = await _source_value(plan.source, context, client)
    except Exception as exc:                     # noqa: BLE001 — recorded
        return Outcome(failure=_failure(exc, plan.source.describe()),
                       at=context)
    return await _run_segments(plan.steps, start, client, row)


async def _source_value(source: Source, context: Any, client: Any) -> Any:
    if source.kind == "context":
        if context is None:
            raise PlanError("this plan needs a context; none was given")
        return context
    if source.kind == "reference":
        from .reference import Reference
        # The source is the *reference*; a `resolve()` step does the
        # resolving, so `ref(url)` and `ref(url).resolve()` mean what they say.
        return Reference.from_url(source.url or "").bind(
            client or _default_client())
    if source.kind == "document":
        target = client or _default_client()
        found = target.document(source.document_id or "")
        if found is None:
            raise PlanError(
                f"no document {source.document_id!r} in this client's registry")
        return found
    raise PlanError(f"unknown plan source {source.kind!r}")


def _default_client() -> Any:
    from .client import default_client
    return default_client()


async def _run_segments(steps: Sequence[Any], value: Any, client: Any,
                        row: Mapping[str, Any] | None) -> Outcome:
    """Split on `otherwise`, which recovers everything recorded before it."""
    index = next((i for i, s in enumerate(steps)
                  if isinstance(s, OtherwiseStep)), None)
    if index is None:
        return await _run_steps(steps, value, client, row)
    prefix, step, rest = steps[:index], steps[index], steps[index + 1:]
    outcome = await _run_steps(prefix, value, client, row)
    outcome = await apply_otherwise(outcome, step, client, row)
    if not outcome.ok:
        return outcome
    return await _run_segments(rest, outcome.value, client, row)


async def _run_steps(steps: Sequence[Any], value: Any, client: Any,
                     row: Mapping[str, Any] | None) -> Outcome:
    for step in steps:
        try:
            value = await _apply(step, value, client, row)
        except _Dropped:
            return Outcome(dropped=True, at=value)
        except PlanError:
            raise
        except Exception as exc:                 # noqa: BLE001 — recorded
            return Outcome(failure=_failure(exc, _describe(step)),
                           at=_failure_context(exc, value))
    return Outcome(value=value)


def _failure(exc: BaseException, where: str) -> Failure:
    return Failure(message=str(exc) or type(exc).__name__, op=where,
                   kind=type(exc).__name__, error=exc)


def _failure_context(exc: BaseException, value: Any) -> Any:
    """A failed resolve still has a Document worth projecting from."""
    return getattr(exc, "document", None) or value


# --------------------------------------------------------------------------- #
# Step dispatch — exhaustive by construction
# --------------------------------------------------------------------------- #

async def _apply(step: Any, value: Any, client: Any,
                 row: Mapping[str, Any] | None) -> Any:
    if isinstance(step, CallStep):
        return await _call(step, value, client, row)
    if isinstance(step, BinOpStep):
        return await _binop(step, value, client, row)
    if isinstance(step, ThenStep):
        return await _then(step.fields, value, client, row)
    if isinstance(step, MapStep):
        return await _map(step, value, client, row)
    if isinstance(step, FilterStep):
        return await _filter(step, value, client, row)
    if isinstance(step, ExplodeStep):
        return _explode(step, value)
    if isinstance(step, LimitStep):
        return _limit(step, value)
    from .plan import LiteralStep
    if isinstance(step, LiteralStep):
        return Value(step.value)
    raise PlanError(
        f"no evaluation for plan step {type(step).__name__} — this is a bug; "
        "a step is never skipped silently")


def _describe(step: Any) -> str:
    if isinstance(step, CallStep):
        return f"{step.op}()"
    if isinstance(step, BinOpStep):
        return step.operator
    return type(step).__name__.replace("Step", "").lower()


# -- calls ------------------------------------------------------------------ #

async def _call(step: CallStep, value: Any, client: Any,
                row: Mapping[str, Any] | None) -> Any:
    args = [await _argument(a, value, client, row) for a in step.args]
    kwargs = {k: await _argument(a, value, client, row)
              for k, a in step.kwargs.items()}
    if step.op == "field":
        return _field(args[0] if args else kwargs.get("name"), value, row,
                      bool(kwargs.get("optional", False)))
    receiver = _receiver_for(value, step.op)
    try:
        spec = _spec_for(receiver, step.op)
    except AttributeError as exc:
        raise PlanError(
            f"{type(receiver).__name__} has no op {step.op!r}") from exc
    _check_capability(receiver, spec)
    result = spec.fn(receiver, *args, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


def _receiver_for(value: Any, op_name: str) -> Any:
    """A `Value` holding a Reference must forward `resolve` to it — that is
    what makes `field("link").resolve()` work."""
    if isinstance(value, (Value, Selection)):
        if REGISTRY.lookup(type(value).__name__, op_name) is None:
            inner = value.get()
            if inner is not None:
                return inner
    return value


def _field(name: Any, value: Any, row: Mapping[str, Any] | None,
           optional: bool) -> Value[Any]:
    key = _unwrap(name)
    if row is not None and key in row:
        return Value(row[key])
    store = getattr(value, "_fields", None)
    if store is not None and key in store:
        return Value(store[key])
    if optional:
        return Value(None)
    known = ", ".join(sorted(row or {})) or "none"
    raise WebClientError(
        f"no field {key!r} in this context (have: {known})")


async def _argument(arg: Arg, value: Any, client: Any,
                    row: Mapping[str, Any] | None) -> Any:
    if arg.kind == "literal":
        return arg.value
    if arg.kind == "field":
        return _unwrap(_field(arg.name, value, row, False))
    if arg.kind == "plan" and arg.plan is not None:
        outcome = await _execute(arg.plan, value, client, row)
        return _unwrap(_finish(outcome))
    raise PlanError(f"unknown argument kind {arg.kind!r}")


# -- operators --------------------------------------------------------------- #

async def _binop(step: BinOpStep, value: Any, client: Any,
                 row: Mapping[str, Any] | None) -> Value[Any]:
    left = _unwrap(value)
    right = None
    if step.right is not None:
        right = await _argument(step.right, value, client, row)
    return Value(_EAGER_OPS[step.operator](left, _unwrap(right)))


# -- record constructors ------------------------------------------------------ #

async def _then(fields: Sequence[Projection], value: Any, client: Any,
                row: Mapping[str, Any] | None) -> Record:
    accumulated: dict[str, Any] = dict(row or {})
    produced: dict[str, Any] = {}
    for projection in fields:
        context = _context_for(projection.plan.source, value, None)
        outcome = await _execute(projection.plan, context, client, accumulated)
        if outcome.dropped:
            raise _Dropped()
        if not outcome.ok:
            error = outcome.failure.error if outcome.failure else None
            raise error or WebClientError(
                outcome.failure.message if outcome.failure else "projection failed")
        materialised = _materialise(outcome.value)
        produced[projection.name] = materialised
        accumulated[projection.name] = materialised
    return Record(produced, context=value)


def _context_for(source: Source, value: Any, failure: Failure | None) -> Any:
    """`doc` and `err` are different roots, so a recovery block can project
    from the response *and* from the error."""
    if source.root == "Err":
        return Err(failure)
    return value


async def _map(step: MapStep, value: Any, client: Any,
               row: Mapping[str, Any] | None) -> RecordSet:
    items = list(_iterate(value))
    fanout = _fanout(client)
    rows: list[Record] = []

    async def build(element: Any) -> Any:
        try:
            return await _then(step.fields, element, client, row)
        except _Dropped:
            return _Dropped

    for start in range(0, len(items), fanout):
        chunk = items[start:start + fanout]
        results = await asyncio.gather(*(build(e) for e in chunk),
                                       return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
            if result is not _Dropped:
                rows.append(result)
    return RecordSet(rows)


def _fanout(client: Any) -> int:
    pool = getattr(client, "pool", None)
    return max(1, getattr(pool, "max_http", DEFAULT_FANOUT))


async def _filter(step: FilterStep, value: Any, client: Any,
                  row: Mapping[str, Any] | None) -> Any:
    kept = []
    for element in _iterate(value):
        merged = dict(row or {})
        if isinstance(element, Mapping):
            merged.update(element)
        outcome = await _execute(step.predicate, element, client, merged)
        if outcome.dropped:
            continue
        if not outcome.ok:
            error = outcome.failure.error if outcome.failure else None
            raise error or WebClientError("filter predicate failed")
        if bool(_unwrap(outcome.value)):
            kept.append(element)
    return RecordSet(kept) if isinstance(value, RecordSet) else Selection(kept)


def _explode(step: ExplodeStep, value: Any) -> RecordSet:
    parts = step.path.split(".")
    rows: list[Any] = []

    def walk(record: Any, path: Sequence[str]) -> None:
        if not path:
            rows.append(record)
            return
        head, rest = path[0], path[1:]
        if not isinstance(record, Mapping) or head not in record:
            raise PlanError(f"explode: no column {head!r} to flatten")
        nested = record[head]
        if not isinstance(nested, (list, tuple, RecordSet, Selection)):
            raise PlanError(
                f"explode: column {head!r} is not a collection")
        outer = {k: v for k, v in record.items() if k != head}
        for item in nested:
            merged = dict(outer)
            if isinstance(item, Mapping):
                merged.update(item)
            else:
                merged[head] = item
            walk(merged, rest) if rest else rows.append(Record(merged))

    for record in _iterate(value):
        walk(record, parts)
    return RecordSet(rows)


def _limit(step: LimitStep, value: Any) -> Any:
    items = list(_iterate(value))[:step.count]
    return RecordSet(items) if isinstance(value, RecordSet) else Selection(items)


def _iterate(value: Any) -> Iterable[Any]:
    if isinstance(value, (Selection, RecordSet)):
        return list(value)
    if isinstance(value, Value):
        inner = value.get()
        return list(inner) if isinstance(inner, (list, tuple)) else [inner]
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


# -- otherwise ---------------------------------------------------------------- #

async def apply_otherwise(outcome: Outcome, step: OtherwiseStep, client: Any,
                          row: Mapping[str, Any] | None) -> Outcome:
    """Shared by the evaluator and by `Record.otherwise`, so recovery means
    exactly one thing."""
    if outcome.ok:
        if step.recovery is None:
            return outcome
        return Outcome(value=_tag(outcome.value, True))
    if outcome.dropped:
        return outcome
    if step.sentinel == "raise":
        error = outcome.failure.error if outcome.failure else None
        raise error or WebClientError(
            outcome.failure.message if outcome.failure else "plan failed")
    if step.sentinel == "drop":
        return Outcome(dropped=True)
    if step.sentinel == "null":
        return Outcome(value=None)
    if step.recovery is None:
        raise PlanError("otherwise() has neither a sentinel nor a recovery")
    try:
        recovered = await _then_recovery(step.recovery, outcome, client, row)
    except _Dropped:
        return Outcome(dropped=True)
    return Outcome(value=recovered)


async def _then_recovery(fields: Sequence[Projection], outcome: Outcome,
                         client: Any, row: Mapping[str, Any] | None) -> Record:
    accumulated: dict[str, Any] = dict(row or {})
    produced: dict[str, Any] = {"ok": False}
    for projection in fields:
        context = _context_for(projection.plan.source, outcome.at,
                               outcome.failure)
        result = await _execute(projection.plan, context, client, accumulated)
        materialised = _materialise(result.value) if result.ok else None
        produced[projection.name] = materialised
        accumulated[projection.name] = materialised
    return Record(produced, ok=False, tagged=True, context=outcome.at)


def _tag(value: Any, ok: bool) -> Any:
    if isinstance(value, Record):
        return Record({"ok": ok, **value.to_dict()}, tagged=True, ok=ok,
                      context=value._context)
    if isinstance(value, RecordSet):
        return RecordSet([_tag(r, ok) for r in value])
    return Record({"ok": ok, "value": _materialise(value)}, tagged=True, ok=ok)


# -- materialisation ---------------------------------------------------------- #

def _materialise(value: Any) -> Any:
    if isinstance(value, Value):
        return value.get()
    if isinstance(value, Selection):
        return [_materialise(v) for v in value]
    return value
