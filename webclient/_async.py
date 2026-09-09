"""Backings implement their primitives synchronously (tree) or
asynchronously (page). Surfaces are written once against both."""
from __future__ import annotations

import inspect
from typing import Any, Callable


def chain(value: Any, then: Callable[[Any], Any]) -> Any:
    """Apply `then` to `value`, awaiting it first if it is awaitable.

    Returns a coroutine when `value` is awaitable, which `@op` settles on the
    engine. A tree backing never produces one, so pure selection costs no
    thread hop.
    """
    if inspect.isawaitable(value):
        async def resume() -> Any:
            return then(await value)
        return resume()
    return then(value)


def chain2(value: Any, then: Callable[[Any], Any]) -> Any:
    """`chain`, where `then` may itself return an awaitable."""
    if inspect.isawaitable(value):
        async def resume() -> Any:
            result = then(await value)
            return await result if inspect.isawaitable(result) else result
        return resume()
    return then(value)
