"""The lazy execution engine and the four dispatch modes.

A :class:`Lazy` is a proxy that RECORDS method calls into a :class:`Plan` instead of running
them (``lazy``). Calling a terminal on it runs the plan in one of the other three modes:
``collect`` blocks (``sync``), ``acollect`` awaits (``async``), and ``to_blob`` + :func:`run_blob`
ship the plan to run elsewhere (``API`` / remote). The plain layers wrote each method once,
sync-or-async; the engine dispatches them uniformly -- awaiting a coroutine result, passing a
plain value straight through -- so one definition lights up all four modes.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from web.fetch import Request
from web.resolve import Resolver

from .plan import Plan


class Lazy:
    """A recorder rooted at a plan. Any attribute access returns a call-recorder that appends a
    step and returns a new Lazy, so ``dsl.get(url).select("h1").text()`` builds a plan without
    touching the network. The Lazy itself is the LAZY form; the terminals run it."""

    def __init__(self, engine: "DSL", plan: Plan) -> None:
        self._engine = engine
        self._plan = plan

    def __getattr__(self, name: str) -> "Any":
        if name.startswith("_"):
            raise AttributeError(name)

        def record(*args: Any, **kwargs: Any) -> "Lazy":
            return Lazy(self._engine, self._plan.then(name, args, kwargs))

        return record

    @property
    def plan(self) -> Plan:
        return self._plan

    def to_blob(self) -> str:
        """API/remote dispatch: the serialised plan to run on a server (see :func:`run_blob`)."""
        return self._plan.to_blob()

    async def acollect(self) -> Any:
        """ASYNC dispatch: run the plan on the caller's loop and return the result."""
        return await self._engine.run(self._plan)

    def collect(self) -> Any:
        """SYNC dispatch: run the plan to completion and return the result (blocks; call it from
        non-async code, not from inside a running loop)."""
        return asyncio.run(self._engine.run(self._plan))


class DSL:
    """A lazy execution engine over a :class:`~web.resolve.Resolver`. ``get(url)`` roots a Lazy
    at resolving that URL; recorded steps are method calls applied to the resulting Document."""

    def __init__(self, resolver: Resolver) -> None:
        self._resolver = resolver

    def get(self, url: str) -> Lazy:
        return Lazy(self, Plan(url=url))

    async def run(self, plan: Plan) -> Any:
        """Execute a plan: resolve the root URL, then apply each step -- awaiting a coroutine
        result (an async method) and passing a plain value straight through (a sync method)."""
        obj: Any = await self._resolver.resolve(Request(url=plan.url))
        for step in plan.steps:
            result = getattr(obj, step.op)(*step.args, **step.kwargs)
            obj = await result if inspect.isawaitable(result) else result
        return obj

    async def aclose(self) -> None:
        await self._resolver.aclose()


async def run_blob(blob: str, resolver: Resolver) -> Any:
    """API/remote dispatch, server side: rebuild a plan from its blob and run it against a local
    resolver. A remote client ``to_blob``s a plan and POSTs it; the server calls this. (The
    result is returned live here; serialising it back is the transport's concern.)"""
    return await DSL(resolver).run(Plan.from_blob(blob))


__all__ = ["DSL", "Lazy", "run_blob"]
