"""A generic bounded ``observe -> decide -> apply`` loop -- the shared base for the
interaction loop (``llm.agent.drive``) and, later, the query loop.

The shape mirrors crawl ``step``/``run``: a round is a DECISION, not an atomic action, so a
round's ``decide`` may return one action (interaction), a set of picks (crawl), or a whole
query increment (query loop) -- the base does not care. The base owns only the mechanics
every such loop shares: the round budget, no-progress (stall) detection, exception-to-verdict
capture, and the terminal :class:`LoopVerdict`. Everything domain-specific -- what an
observation is, how a decision is made, how it is applied, when it is "done", and how to
measure progress -- is supplied as callables, so the base stays a minimal scaffold and never
grows a framework.

A specialisation wires the callables and calls :meth:`BoundedLoop.run`; recording is left to
the domain (the interaction loop records through the document's ordinary ops; the query loop
will build an ``Expr``), so the base carries no recording machinery.
"""

from __future__ import annotations

import logging
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel

if True:  # keep the runtime import surface tiny; the callables are structural
    from collections.abc import Callable

log = logging.getLogger(__name__)

S = TypeVar("S")  # the driven STATE (e.g. a live Document, a crawl, a query-in-progress)
O = TypeVar("O")  # an OBSERVATION handed to decide
D = TypeVar("D")  # a DECISION returned by decide


class LoopVerdict(BaseModel):
    """Why a :class:`BoundedLoop` stopped. ``reason``: ``done`` (the decision declared the loop
    finished), ``budget`` (hit ``max_rounds``), ``stalled`` (``max_stalls`` no-progress rounds in
    a row), or ``error`` (``apply`` raised). ``rounds`` is how many rounds ran; ``result`` is the
    done payload; ``error`` the failure message."""

    done: bool
    reason: Literal["done", "budget", "stalled", "error"]
    rounds: int
    result: str = ""
    error: str = ""


class BoundedLoop(Generic[S, O, D]):
    """A reusable bounded loop over a mutating ``state``. Each round: ``observe`` the state,
    ``decide`` from the observation, and unless the decision is terminal, ``apply`` it -- until a
    decision is done, ``apply`` raises, progress stalls, or the round budget is spent.

    Callables:
      * ``observe(state, round_index, last_error) -> O`` -- what the decider sees this round.
      * ``decide(observation) -> D`` -- the "brain" (an LLM adapter, a policy fn, ...).
      * ``done_result(decision) -> str | None`` -- ``None`` keeps going; a string ends the loop
        ``done`` with that result.
      * ``apply(state, decision) -> None`` -- enact a non-terminal decision (mutating ``state``).
      * ``progress(state) -> Any | None`` -- an optional cheap signature of visible progress;
        equal signatures on consecutive rounds count as a stall (``None`` disables stall checks).
    """

    def __init__(
        self,
        *,
        observe: "Callable[[S, int, str], O]",
        decide: "Callable[[O], D]",
        done_result: "Callable[[D], str | None]",
        apply: "Callable[[S, D], None]",
        progress: "Callable[[S], Any] | None" = None,
        max_rounds: "int | None" = None,
        max_stalls: "int | None" = None,
        name: str = "loop",
    ) -> None:
        from .settings import current

        budgets = current().loops
        self._observe = observe
        self._decide = decide
        self._done_result = done_result
        self._apply = apply
        self._progress = progress
        self.max_rounds = budgets.max_rounds if max_rounds is None else max_rounds
        self.max_stalls = budgets.max_stalls if max_stalls is None else max_stalls
        self.name = name

    def run(self, state: S) -> LoopVerdict:
        """Drive ``state`` round by round until a terminal condition, returning the verdict.
        Terminates on: a done decision (``done``), an ``apply`` exception (``error``, captured
        never raised), ``max_stalls`` consecutive no-progress rounds (``stalled``), or
        ``max_rounds`` (``budget``)."""
        stalls = 0
        prev: Any = _UNSET
        error = ""
        for i in range(self.max_rounds):
            decision = self._decide(self._observe(state, i, error))
            log.debug("%s round %d/%d: %r", self.name, i + 1, self.max_rounds, decision)
            result = self._done_result(decision)
            if result is not None:
                log.info("%s done after %d round(s)", self.name, i)
                return LoopVerdict(done=True, reason="done", rounds=i, result=result)
            try:
                self._apply(state, decision)
                error = ""
            except Exception as exc:  # noqa: BLE001 - surface it to the verdict, never crash
                log.warning("%s stopped on error at round %d: %s", self.name, i + 1, exc)
                return LoopVerdict(done=False, reason="error", rounds=i, error=str(exc))
            if self._progress is not None:
                current = self._progress(state)
                stalls = stalls + 1 if current == prev else 0
                prev = current
                if stalls >= self.max_stalls:
                    log.info("%s stalled after %d round(s)", self.name, i + 1)
                    return LoopVerdict(done=False, reason="stalled", rounds=i + 1)
        log.info("%s hit its round budget (%d)", self.name, self.max_rounds)
        return LoopVerdict(done=False, reason="budget", rounds=self.max_rounds)


_UNSET: Any = object()  # a first-round sentinel that no progress signature can equal


__all__ = ["BoundedLoop", "LoopVerdict"]
