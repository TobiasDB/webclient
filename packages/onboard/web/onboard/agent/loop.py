"""``BoundedLoop`` -- the ``observe -> decide -> apply`` primitive, at the tier that actually needs
it: the agent loop.

Unlike the simple bounded ``for``/``while`` loops in the lower layers (retry, pagination, crawl),
an agent loop needs the parts BoundedLoop owns: a round budget, no-progress (stall) detection, and
-- the reason it exists -- **interrupt / resume**. A driver's :meth:`decide` may return an
:class:`Ask` instead of a decision; the loop then stops with ``reason="waiting"`` and the caller
:meth:`resume`s it with a human answer, continuing from the same round. That is the human-in-the-
loop primitive every agent framework converges on (LangGraph ``interrupt`` / Stagehand ``askHuman``).

Everything domain-specific -- what an observation is, how a decision is made and applied, when it
is done -- is a plain callable, so the base stays a small scaffold. Async-native (agents do IO).
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, JsonValue

S = TypeVar("S")  # the driven state
O = TypeVar("O")  # an observation of it
D = TypeVar("D")  # a decision made from the observation
_T = TypeVar("_T")

Reason = Literal["done", "budget", "stalled", "error", "waiting"]


async def _resolve(value: "_T | Awaitable[_T]") -> _T:
    """Await ``value`` if it is a coroutine, else return it -- so observe/decide may be sync or
    async and the loop handles both through one path."""
    if inspect.isawaitable(value):
        return await value
    return value


class Done(BaseModel):
    """The shared terminal marker: a driver/policy returns it to end the loop successfully (the
    current state is the answer). Loop vocabulary lives here, so every agent uses the one ``Done``.
    """


class Ask(BaseModel):
    """A driver's request for a HUMAN decision, returned from ``decide`` in place of a decision.
    The loop checkpoints (``reason="waiting"``) and resumes with the answer."""

    reason: str
    options: list[JsonValue] = []
    detail: dict[str, JsonValue] = {}


class Verdict(BaseModel):
    """Why the loop stopped. ``done`` (a decision was terminal), ``budget``/``stalled`` (bounds),
    ``error`` (apply raised), or ``waiting`` (the driver asked -- ``ask`` holds the question).
    """

    reason: Reason
    rounds: int
    error: str = ""
    ask: "Ask | None" = None

    @property
    def ok(self) -> bool:
        return self.reason == "done"


class _Unset:
    __slots__ = ()


_UNSET = _Unset()


class BoundedLoop(Generic[S, O, D]):
    """A bounded, resumable ``observe -> decide -> apply`` loop. ``done`` marks a decision terminal;
    ``progress`` (optional) maps state to a value compared across rounds for stall detection.
    """

    _state: S  # set by arun() before _drive()/resume() ever read it

    def __init__(
        self,
        *,
        observe: "Callable[[S], O | Awaitable[O]]",
        decide: "Callable[[O], D | Ask | Awaitable[D | Ask]]",
        apply: "Callable[[S, D], None | Awaitable[None]]",
        done: "Callable[[D], bool]",
        progress: "Callable[[S], object] | None" = None,
        max_rounds: int = 20,
        max_stalls: int = 3,
    ) -> None:
        self._observe, self._decide, self._apply, self._done = (
            observe,
            decide,
            apply,
            done,
        )
        self._progress = progress
        self.max_rounds, self.max_stalls = max_rounds, max_stalls
        self.round = 0
        self._stalls = 0
        self._prev: object = _UNSET
        self._waiting = False

    async def arun(self, state: S) -> Verdict:
        """Drive from ``state`` to a verdict (which may be ``waiting`` -- then call :meth:`resume`)."""
        self._state = state
        return await self._drive()

    async def resume(self, answer: D) -> Verdict:
        """Continue a ``waiting`` loop: use ``answer`` as the decision for the checkpointed round,
        apply it, and drive on."""
        if not self._waiting:
            raise RuntimeError("resume() called but the loop is not waiting")
        self._waiting = False
        if self._done(answer):
            return self._verdict("done")
        try:
            await self._apply_decision(answer)
        except Exception as exc:
            return self._verdict("error", error=str(exc))
        if (v := self._advance()) is not None:
            return v
        return await self._drive()

    async def _drive(self) -> Verdict:
        while True:
            if self.round >= self.max_rounds:
                return self._verdict("budget")
            observation: O = await _resolve(self._observe(self._state))  # sync or async observe
            decision: "D | Ask" = await _resolve(self._decide(observation))  # sync or async driver
            if isinstance(decision, Ask):
                self._waiting = True
                return self._verdict("waiting", ask=decision)
            if self._done(decision):
                return self._verdict("done")
            try:
                await self._apply_decision(decision)
            except Exception as exc:
                return self._verdict("error", error=str(exc))
            if (v := self._advance()) is not None:
                return v

    async def _apply_decision(self, decision: D) -> None:
        result = self._apply(self._state, decision)
        if result is not None:
            await result

    def _advance(self) -> "Verdict | None":
        self.round += 1
        if self._progress is not None:
            mark = self._progress(self._state)
            self._stalls = self._stalls + 1 if mark == self._prev else 0
            self._prev = mark
            if self._stalls >= self.max_stalls:
                return self._verdict("stalled")
        return None

    def _verdict(self, reason: Reason, *, error: str = "", ask: "Ask | None" = None) -> Verdict:
        return Verdict(reason=reason, rounds=self.round, error=error, ask=ask)


__all__ = ["BoundedLoop", "Verdict", "Ask", "Done", "Reason"]
