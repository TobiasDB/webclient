"""Per-WebClient asyncio loop in a daemon thread (ISSUES #18), with the
sync<->async bridge. All engine I/O runs here; public facade methods wrap
coroutines via ``run``.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, AsyncIterator, Coroutine, Iterator, TypeVar

T = TypeVar("T")

_SENTINEL = object()


class EngineLoop:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        #: set once ``stop()`` begins tearing the loop down, BEFORE the drain. A
        #: cross-thread ``run`` that raced past the ``closed`` check would
        #: otherwise submit a coroutine onto a loop that is about to stop running
        #: and then block forever (``timeout=None``) on a result that never
        #: arrives. A stopping loop is a closed loop for the purpose of accepting
        #: new blocking work.
        self._stopping = False
        self._thread = threading.Thread(
            target=self._main, name="webclient-engine", daemon=True
        )
        self._thread.start()

    def _main(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    @property
    def closed(self) -> bool:
        return not self._thread.is_alive()

    def on_loop_thread(self) -> bool:
        """True when the caller is already on the engine loop (the evaluator),
        so a coroutine should be awaited rather than bridged."""
        return threading.current_thread() is self._thread

    def submit(self, coro: Coroutine[Any, Any, T]) -> Any:
        """Schedule ``coro`` on the loop and return its ``concurrent.futures``
        Future (for an async caller to ``wrap_future`` and await without
        blocking its own loop)."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def run(self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        # Re-entrancy guard, unconditional (ISSUES #28): a bus handler runs
        # on this thread and must never block on it.
        if threading.current_thread() is self._thread:
            coro.close()
            raise RuntimeError(
                "sync facade method called from the engine loop thread -- "
                "bus handlers must not call facade methods"
            )
        if self.closed or self._stopping:
            coro.close()
            raise RuntimeError("engine loop is stopped (WebClient closed?)")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def stream(self, source: AsyncIterator[T], buffer: int = 8) -> Iterator[T]:
        """Bridge an async iterator into a blocking sync iterator.

        The queue is loop-native (``asyncio.Queue``): ``_pump`` awaits
        ``put``/``get`` on the loop, so it is cleanly cancellable no matter
        how the consumer stops -- fully drained, broken early, or abandoned
        to GC. Backpressure comes from the bounded queue; the consumer pulls
        each item with its own ``run_coroutine_threadsafe(get())``.
        """
        q: asyncio.Queue[Any] = asyncio.Queue(maxsize=max(1, buffer))
        box: dict[str, Any] = {}

        async def _pump() -> None:
            box["task"] = asyncio.current_task()
            try:
                async for item in source:
                    await q.put(item)
                await q.put(_SENTINEL)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # surfaced on the consuming side
                await q.put(exc)

        asyncio.run_coroutine_threadsafe(_pump(), self._loop)
        try:
            while True:
                if self.closed:
                    break
                item = asyncio.run_coroutine_threadsafe(q.get(), self._loop).result()
                if item is _SENTINEL:
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            # Cancel the pump AND wait for it to finish, so it can never
            # survive the consumer. The CancelledError is thrown into the
            # suspended source generator at its await point, running its own
            # finally (releasing leases, publishing done).
            async def _shutdown() -> None:
                task = box.get("task")
                if task is not None and not task.done():
                    task.cancel()
                    try:
                        await task
                    except BaseException:
                        pass

            if not self.closed:
                try:
                    asyncio.run_coroutine_threadsafe(_shutdown(), self._loop).result(
                        timeout=5
                    )
                except Exception:
                    pass

    async def astream(
        self, source: AsyncIterator[T], buffer: int = 8
    ) -> AsyncIterator[T]:
        """Bridge an engine-loop async iterator to a *caller-loop* async
        iterator, delivering items as they arrive. The async twin of ``stream``:
        the queue and pump live on the engine loop (where ``source`` runs its
        I/O); the caller awaits each item via ``wrap_future`` without blocking
        its own loop. Abandoning the iterator cancels the pump, unwinding
        ``source`` at its await point (releasing leases, publishing done)."""
        box: dict[str, Any] = {}

        async def _setup() -> None:
            q: asyncio.Queue[Any] = asyncio.Queue(maxsize=max(1, buffer))
            box["q"] = q

            async def _pump() -> None:
                box["task"] = asyncio.current_task()
                try:
                    async for item in source:
                        await q.put(item)
                    await q.put(_SENTINEL)
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:  # surfaced on the consuming side
                    await q.put(exc)

            asyncio.ensure_future(_pump())

        await asyncio.wrap_future(self.submit(_setup()))
        q = box["q"]
        try:
            while True:
                if self.closed:
                    break
                item = await asyncio.wrap_future(self.submit(q.get()))
                if item is _SENTINEL:
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:

            async def _shutdown() -> None:
                task = box.get("task")
                if task is not None and not task.done():
                    task.cancel()
                    try:
                        await task
                    except BaseException:
                        pass

            if not self.closed:
                try:
                    await asyncio.wrap_future(self.submit(_shutdown()))
                except Exception:
                    pass

    def stop(self) -> None:
        if not self.closed:
            # Refuse new blocking work from other threads before we start
            # tearing down, so a racing ``run`` cannot strand a coroutine on the
            # stopping loop (see ``_stopping``).
            self._stopping = True
            # Cancel every outstanding task and let the loop settle them so
            # none is "destroyed while pending" when the loop closes.
            done = threading.Event()

            async def _drain() -> None:
                current = asyncio.current_task()
                # Drain in rounds until nothing but ourselves is left: cancelling
                # a task can run a ``finally`` that schedules NEW tasks (a lease
                # release, a re-fetch), and a cross-thread submit can land a task
                # after an earlier round's snapshot; a single pass would strand
                # those and they would be "destroyed while pending" once the loop
                # closes. Bounded so a task that reschedules itself forever cannot
                # wedge shutdown.
                for _ in range(1000):
                    pending = [t for t in asyncio.all_tasks() if t is not current]
                    if not pending:
                        break
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                self._loop.stop()
                done.set()

            asyncio.run_coroutine_threadsafe(_drain(), self._loop)
            done.wait(timeout=5)
            self._thread.join(timeout=5)
        if not self._loop.is_running():
            self._loop.close()
