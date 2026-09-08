"""Per-WebClient asyncio loop in a daemon thread (ISSUES #18), with the
sync<->async bridge. All engine I/O runs here; public facade methods wrap
coroutines via ``run``.
"""
from __future__ import annotations

import asyncio
import queue
import threading
from typing import Any, AsyncIterator, Coroutine, Iterator, TypeVar

T = TypeVar("T")

_SENTINEL = object()


class EngineLoop:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._main, name="webclient-engine", daemon=True)
        self._thread.start()

    def _main(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    @property
    def closed(self) -> bool:
        return not self._thread.is_alive()

    def run(self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        # Re-entrancy guard, unconditional (ISSUES #28): a bus handler runs
        # on this thread and must never block on it.
        if threading.current_thread() is self._thread:
            coro.close()
            raise RuntimeError(
                "sync facade method called from the engine loop thread -- "
                "bus handlers must not call facade methods")
        if self.closed:
            coro.close()
            raise RuntimeError("engine loop is stopped (WebClient closed?)")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def stream(self, source: AsyncIterator[T], buffer: int = 8) -> Iterator[T]:
        """Bridge an async iterator into a blocking sync iterator. Bounded
        queue = backpressure onto the producer."""
        out: queue.Queue[Any] = queue.Queue(maxsize=buffer)

        async def _pump() -> None:
            try:
                async for item in source:
                    await asyncio.get_event_loop().run_in_executor(None, out.put, item)
                await asyncio.get_event_loop().run_in_executor(None, out.put, _SENTINEL)
            except BaseException as exc:  # surfaced on the consuming side
                await asyncio.get_event_loop().run_in_executor(None, out.put, exc)

        future = asyncio.run_coroutine_threadsafe(_pump(), self._loop)
        try:
            while True:
                item = out.get()
                if item is _SENTINEL:
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            future.cancel()

    def stop(self) -> None:
        if not self.closed:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
        if not self._loop.is_running():
            self._loop.close()
