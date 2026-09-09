"""The async engine behind the one public interface.

Op implementations that do I/O are `async def`; the sync surface runs them on
a shared anyio blocking portal. Pure ops never reach here, so selection and
attribute access cost no thread hop — only real I/O does.

There is no `webclient.aio` to import. Callers already inside an event loop
reach the same implementations through `.core` (see `client.py`).
"""
from __future__ import annotations

import atexit
import threading
from typing import Any, Awaitable, Coroutine, TypeVar

from anyio.from_thread import BlockingPortal, start_blocking_portal

from .errors import WebClientError

T = TypeVar("T")

_lock = threading.Lock()
_portal: BlockingPortal | None = None
_portal_cm: Any = None
_portal_thread: threading.Thread | None = None


def portal() -> BlockingPortal:
    """The shared portal, started on first use."""
    global _portal, _portal_cm, _portal_thread
    with _lock:
        if _portal is None:
            _portal_cm = start_blocking_portal()
            _portal = _portal_cm.__enter__()
            _portal_thread = _portal.call(threading.current_thread)
        return _portal


def in_engine_thread() -> bool:
    return (_portal_thread is not None
            and threading.current_thread() is _portal_thread)


async def _await(awaitable: Awaitable[T]) -> T:
    return await awaitable


def run_sync(coro: Coroutine[Any, Any, T]) -> T:
    """Run an engine coroutine from the sync surface and return its result."""
    if in_engine_thread():
        coro.close()
        raise WebClientError(
            "a synchronous op was called from inside the event loop. Use the "
            "async pass-through instead: `await wc.core.<op>(...)` / "
            "`await doc.core.<op>(...)`.")
    return portal().call(_await, coro)


def shutdown() -> None:
    global _portal, _portal_cm, _portal_thread
    with _lock:
        if _portal_cm is not None:
            try:
                _portal_cm.__exit__(None, None, None)
            except Exception:
                pass
        _portal, _portal_cm, _portal_thread = None, None, None


atexit.register(shutdown)
