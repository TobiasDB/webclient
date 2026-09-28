"""The bounded loop: the one ``observe -> decide -> apply`` primitive every higher layer's
"auto" behaviour is built on (resolve's escalation ladder, pagination, crawl, interaction).

The loop owns only what all of them share: a round budget, no-progress (stall) detection, and
the terminal :class:`Verdict`. Everything domain-specific -- what an observation is, how a
decision is made, how it is applied, whether it is terminal, how to measure progress -- is a
plain callable, so the base stays a small scaffold and never grows a framework. It runs sync
(:meth:`run`) or async (:meth:`arun`, awaiting an async ``apply``); both share :meth:`step`,
so a loop can also be stepped and inspected by hand.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel

from .events import Event, EventBus

S = TypeVar("S")  # the driven state
O = TypeVar("O")  # an observation of it
D = TypeVar("D")  # a decision made from the observation

Reason = Literal["done", "budget", "stalled", "error"]


class Verdict(BaseModel):
    """Why a :class:`BoundedLoop` stopped: ``done`` (a decision was terminal), ``budget``
    (hit ``max_rounds``), ``stalled`` (``max_stalls`` no-progress rounds), or ``error``
    (``apply`` raised). ``rounds`` is how many completed; ``error`` carries the message."""

    reason: Reason
    rounds: int
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.reason == "done"


class LoopEvent(Event):
    """Published per round when a loop is given a bus (observation only)."""

    topic: str = "loop"
    name: str = "loop"
    round: int = 0
    reason: str | None = None  # set on the terminal event


class BoundedLoop(Generic[S, O, D]):
    """A bounded ``observe -> decide -> apply`` loop. ``done`` marks a decision terminal;
    ``progress`` (optional) maps state to a value compared across rounds for stall detection."""

    def __init__(
        self,
        *,
        observe: "Callable[[S], O]",
        decide: "Callable[[O], D]",
        apply: "Callable[[S, D], None | Awaitable[None]]",
        done: "Callable[[D], bool]",
        progress: "Callable[[S], Any] | None" = None,
        max_rounds: int = 20,
        max_stalls: int = 3,
        name: str = "loop",
        bus: EventBus | None = None,
    ) -> None:
        self._observe, self._decide, self._apply = observe, decide, apply
        self._done, self._progress = done, progress
        self.max_rounds, self.max_stalls = max_rounds, max_stalls
        self.name, self.bus = name, bus
        self.round = 0
        self._stalls = 0
        self._prev: Any = _UNSET

    def _emit(self, round: int, reason: str | None = None) -> None:
        if self.bus is not None:
            self.bus.publish(LoopEvent(name=self.name, round=round, reason=reason))

    def _budget(self) -> "Verdict | None":
        if self.round >= self.max_rounds:
            return self._finish("budget")
        return None

    def _decision(self, state: S) -> "tuple[D, Verdict | None]":
        """observe -> decide; a terminal decision short-circuits before apply."""
        decision = self._decide(self._observe(state))
        if self._done(decision):
            return decision, self._finish("done")
        return decision, None

    def _advance(self, state: S) -> "Verdict | None":
        """After apply: count the round, check progress for a stall."""
        self.round += 1
        if self._progress is not None:
            mark = self._progress(state)
            self._stalls = self._stalls + 1 if mark == self._prev else 0
            self._prev = mark
            if self._stalls >= self.max_stalls:
                return self._finish("stalled")
        return None

    def _finish(self, reason: Reason, error: str = "") -> Verdict:
        self._emit(self.round, reason)
        return Verdict(reason=reason, rounds=self.round, error=error)

    def run(self, state: S) -> Verdict:
        """Drive the loop to a verdict with a synchronous ``apply``."""
        while True:
            if (v := self._budget()) is not None:
                return v
            self._emit(self.round + 1)
            decision, v = self._decision(state)
            if v is not None:
                return v
            try:
                self._apply(state, decision)  # sync run: apply returns None
            except Exception as exc:
                return self._finish("error", str(exc))
            if (v := self._advance(state)) is not None:
                return v

    async def arun(self, state: S) -> Verdict:
        """Drive the loop to a verdict, awaiting an async ``apply``."""
        while True:
            if (v := self._budget()) is not None:
                return v
            self._emit(self.round + 1)
            decision, v = self._decision(state)
            if v is not None:
                return v
            try:
                result = self._apply(state, decision)
                if result is not None:
                    await result
            except Exception as exc:
                return self._finish("error", str(exc))
            if (v := self._advance(state)) is not None:
                return v


class _Unset:
    __slots__ = ()


_UNSET = _Unset()  # a first-round sentinel distinct from any real progress mark


__all__ = ["BoundedLoop", "Verdict", "LoopEvent", "Reason"]
