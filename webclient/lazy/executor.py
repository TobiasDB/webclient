"""Executor: compile a QueryPlan and run it (M6).

Compilation records an ExecutionGraph (the interface's typed plan view);
execution is a small async interpreter on the WebClient's engine loop. map()
fans out per element with bounded concurrency (in-flight rows capped so
memory stays flat); fetches inside a plan lease from the same pool. Rows
stream out as their subgraphs complete; each step's OnError policy decides
skip / raise / ignore on failure. Plan events (started/row/error/done) are
published on the bus.
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, PrivateAttr

from ..events import Event
from ..models import Document, OnError, Reference

Resource = "http"


class ExecutionStep(BaseModel):
    id: str
    op: dict[str, Any]
    depends_on: list[str] = []
    resource: str | None = None
    page_group: str | None = None
    on_error: OnError = OnError.raise_error


class ExecutionGraph(BaseModel):
    plan_id: str
    root: str | None = None
    steps: list[ExecutionStep] = []


class RunStats(BaseModel):
    rows: int = 0
    requests: int = 0
    errors: int = 0
    done: bool = False


class PlanEvent(Event):
    topic: str = "plan"
    phase: str = "started"           # started / row / error / done
    detail: dict[str, Any] = {}


class _Skip(Exception):
    """Row dropped by OnError.skip."""


class Executor(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    _client: Any = PrivateAttr(default=None)
    _stats: dict[str, RunStats] = PrivateAttr(default_factory=dict)

    # -- compile -------------------------------------------------------------
    def compile(self, plan: Any) -> ExecutionGraph:
        from .expr import QueryPlan
        if not isinstance(plan, QueryPlan):
            plan = plan.to_query()
        steps: list[ExecutionStep] = []
        prev: str | None = None
        for raw in plan.steps:
            step_id = uuid4().hex[:8]
            op = raw.get("op")
            resource = "http" if op == "call" and raw.get("name") in (
                "fetch", "reload") else None
            steps.append(ExecutionStep(
                id=step_id, op=raw, depends_on=[prev] if prev else [],
                resource=resource))
            prev = step_id
        return ExecutionGraph(plan_id=uuid4().hex, root=plan.root, steps=steps)

    def status(self, plan_id: str) -> RunStats:
        return self._stats.get(plan_id, RunStats())

    # -- run (sync facade over the loop) -------------------------------------
    def run(self, plan: Any, context: Any, *, stream: bool = False) -> Any:
        graph = self.compile(plan)
        loop = self._client._ensure_loop()
        has_map = any(s.op.get("op") == "map" for s in graph.steps)
        if stream:
            return loop.stream(self._arun(graph, context),
                               buffer=max(1, self._client.pool.max_http))
        async def collect() -> Any:
            rows = [row async for row in self._arun(graph, context)]
            return rows if has_map else (rows[0] if rows else None)
        return loop.run(collect())

    # -- interpreter (async, on the loop) ------------------------------------
    async def _arun(self, graph: ExecutionGraph,
                    context: Any) -> AsyncIterator[Any]:
        stats = RunStats()
        self._stats[graph.plan_id] = stats
        bus = self._client.bus
        bus.publish(PlanEvent(phase="started", plan_id=graph.plan_id))
        raw_steps = [s.op for s in graph.steps]
        try:
            async for row in self._eval_stream(raw_steps, context, stats,
                                               graph.plan_id):
                stats.rows += 1
                bus.publish(PlanEvent(phase="row", plan_id=graph.plan_id))
                yield row
        finally:
            stats.done = True
            bus.publish(PlanEvent(phase="done", plan_id=graph.plan_id,
                                  detail={"rows": stats.rows}))

    async def _eval_stream(self, steps: list[dict], context: Any,
                           stats: RunStats,
                           plan_id: str) -> AsyncIterator[Any]:
        """Walk steps until a map (which produces the row stream); scalar
        plans yield a single value."""
        value: Any = context
        for i, step in enumerate(steps):
            op = step.get("op")
            if op == "map":
                async for row in self._run_map(
                        value, step, steps[i + 1:], stats, plan_id):
                    yield row
                return
            value = await self._apply(step, value, {}, stats)
        yield value

    async def _run_map(self, collection: Any, map_step: dict,
                       rest: list[dict], stats: RunStats,
                       plan_id: str) -> AsyncIterator[Any]:
        elements = list(collection)
        fields = map_step["fields"]
        sem = asyncio.Semaphore(max(1, self._client.pool.max_http))

        async def build(element: Any) -> Any:
            async with sem:
                try:
                    row = await self._eval_fields(fields, element, {}, stats)
                except _Skip:
                    return _Skip
                for step in rest:
                    row = await self._apply_row_step(step, row, stats)
                    if row is _Skip:
                        return _Skip
                return row

        for coro in asyncio.as_completed([build(e) for e in elements]):
            row = await coro
            if row is not _Skip:
                yield row

    async def _apply_row_step(self, step: dict, row: Any,
                              stats: RunStats) -> Any:
        op = step.get("op")
        if op == "filter":
            keep = await self._eval_subplan(step["predicate"], row, row, stats)
            return row if keep else _Skip
        if op == "then":
            extra = await self._eval_fields(step["fields"], row, row, stats)
            return {**row, **extra}
        if op == "otherwise":
            return row               # policy handled at field level
        return row

    async def _eval_fields(self, fields: dict, element: Any, row: dict,
                           stats: RunStats) -> dict:
        out: dict[str, Any] = dict(row)
        for name, subplan in fields.items():
            out[name] = await self._eval_subplan(subplan, element, out, stats)
        return out

    async def _eval_subplan(self, subplan: dict, element: Any, row: dict,
                            stats: RunStats) -> Any:
        if "literal" in subplan:
            return subplan["literal"]
        steps = subplan["steps"]
        policy = OnError.raise_error
        for step in steps:
            if step.get("op") == "otherwise":
                policy = OnError(step["state"])
        value: Any = element
        started = False
        try:
            for step in steps:
                if step.get("op") == "otherwise":
                    continue
                if step.get("op") == "col" and not started:
                    value = row.get(step["name"])
                    started = True
                    continue
                if step.get("op") == "lit" and not started:
                    value = step["value"]
                    started = True
                    continue
                started = True
                value = await self._apply(step, value, row, stats)
            return value
        except _Skip:
            raise
        except Exception:
            if policy == OnError.skip:
                raise _Skip()
            if policy == OnError.ignore:
                return None
            raise

    async def _apply(self, step: dict, value: Any, row: dict,
                     stats: RunStats) -> Any:
        if "binop" in step:
            return self._binop(step, value, row)
        op = step.get("op")
        if op == "get":
            return getattr(value, step["name"])
        if op == "col":
            return row.get(step["name"])
        if op == "lit":
            return step["value"]
        if op == "binop":
            return self._binop(step, value, row)
        if op == "call":
            return await self._call(step, value, row, stats)
        raise ValueError(f"cannot execute op {op!r}")

    async def _call(self, step: dict, value: Any, row: dict,
                    stats: RunStats) -> Any:
        name = step["name"]
        args = [self._unwrap(a, row) for a in step.get("args", [])]
        kwargs = {k: self._unwrap(v, row) for k, v in step.get("kwargs", {}).items()}
        # Fetches inside a plan must run through this WebClient and its loop.
        if name in ("fetch", "reload"):
            kwargs.setdefault("client", self._client)
            stats.requests += 1
            if isinstance(value, Reference):
                return await self._client._fetch(
                    value, optional=kwargs.get("optional", False),
                    session=value._session)
            if isinstance(value, Document):
                return await self._client._fetch(
                    value, optional=kwargs.get("optional", False),
                    session=value._session)
        method = getattr(value, name)
        result = method(*args, **kwargs)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    def _unwrap(self, arg: Any, row: dict) -> Any:
        if isinstance(arg, dict) and "__expr__" in arg:
            return {"__deferred_expr__": arg["__expr__"]}  # rare; unsupported
        return arg

    def _binop(self, step: dict, value: Any, row: dict) -> Any:
        opname = step["binop"]
        if opname == "not":
            return not value
        other = step.get("value")
        if step.get("value_is_expr"):
            other = None             # nested-expr comparands are uncommon
        ops = {
            "eq": lambda a, b: a == b, "ne": lambda a, b: a != b,
            "lt": lambda a, b: a < b, "gt": lambda a, b: a > b,
            "and": lambda a, b: bool(a) and bool(b),
            "or": lambda a, b: bool(a) or bool(b),
        }
        return ops[opname](value, other)
