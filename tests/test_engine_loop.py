"""Regression tests for EngineLoop teardown (webclient/core/client/loop.py).

Concurrency/shutdown invariants, tested against the loop directly (no client):

* ``stop()`` must settle EVERY outstanding task -- including ones a cancelled
  task schedules from its ``finally`` during the drain -- so none is left
  "destroyed while pending" when the loop closes.
* Once ``stop()`` begins tearing the loop down, a cross-thread ``run`` must be
  refused cleanly rather than block forever on a coroutine the stopping loop
  will never run.
"""

import asyncio
import logging

from webclient.core.client.loop import EngineLoop


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_stop_settles_tasks_scheduled_during_drain() -> None:
    # A cancelled task whose finally schedules a NEW task (a lease release, a
    # re-fetch) used to be stranded: stop() cancelled a single snapshot, then
    # closed the loop under the freshly-scheduled task -> "Task was destroyed
    # but it is pending!". The drain now loops until nothing remains.
    cap = _Capture()
    log = logging.getLogger("asyncio")
    log.addHandler(cap)
    old_level = log.level
    log.setLevel(logging.DEBUG)
    try:
        eng = EngineLoop()

        async def leftover() -> None:
            await asyncio.sleep(100)

        async def worker() -> None:
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                asyncio.ensure_future(leftover())  # scheduled mid-drain
                raise

        done = asyncio.run_coroutine_threadsafe(
            asyncio.sleep(0), eng._loop
        )  # ensure the loop is spinning
        done.result(timeout=5)
        eng._loop.call_soon_threadsafe(lambda: asyncio.ensure_future(worker()))
        # let worker suspend on its sleep before we tear down
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0.1), eng._loop).result(5)

        eng.stop()
    finally:
        log.removeHandler(cap)
        log.setLevel(old_level)

    stranded = [m for m in cap.messages if "destroyed but it is pending" in m]
    assert stranded == [], stranded


def test_run_refused_once_stopping_without_hanging() -> None:
    # A run() that races past the closed check while stop() is tearing down must
    # not submit onto the stopping loop and block forever; the _stopping guard
    # rejects it cleanly (and closes the coroutine, so no "never awaited").
    eng = EngineLoop()
    eng._stopping = True

    async def io() -> int:
        return 1

    coro = io()
    try:
        raised = False
        try:
            eng.run(coro, timeout=2)
        except RuntimeError:
            raised = True
        assert raised
        # the rejected coroutine was closed, not leaked half-awaited
        assert coro.cr_running is False
    finally:
        eng._stopping = False
        eng.stop()
