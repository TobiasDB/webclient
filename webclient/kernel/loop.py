"""The bounded ``observe -> decide -> apply`` loop -- the ONE loop concept every "auto"
mode in the package is built on (roadmap N9): the interaction loop, the query loop, the
crawl drive, the resolve escalation ladder and the locate loop.

A round is a DECISION, not an atomic action: ``decide`` may return one action
(interaction), a set of picks (crawl), a query increment (query loop) or a transport tier
(resolve) -- the base does not care. The base owns only what every such loop shares:

* the round budget and no-progress (stall) detection, and the terminal :class:`LoopVerdict`;
* the **driver** seam -- ``decide`` is any callable, so the auto backend is swappable
  (an LLM adapter, a heuristic, a test stub) and a loop can always be driven by hand;
* **manual mode** -- :meth:`BoundedLoop.step` runs ONE round with a caller-supplied
  decision (or the driver's), so a loop can be stepped, inspected and steered;
* **interrupt / resume** -- a driver that returns an :class:`Ask` checkpoints the loop:
  ``run`` stops with ``reason="waiting"`` (the question on ``verdict.ask``), and
  :meth:`BoundedLoop.resume` continues from the same round with the answer -- the
  human-in-the-loop primitive every framework converges on (LangGraph ``interrupt`` /
  Stagehand ``askHuman``);
* **traceability** -- given a ``bus`` it publishes a :class:`~webclient.models.LoopEvent`
  per round / decision / checkpoint / verdict, so a trace or UI can draw it.

Everything domain-specific -- what an observation is, how a decision is made, how it is
applied, when it is "done", how to measure progress -- is supplied as callables, so the
base stays a minimal scaffold and never grows a framework.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Awaitable, Generic, Literal, Protocol, TypeVar, cast, runtime_checkable

from pydantic import BaseModel

if True:  # keep the runtime import surface tiny; the callables are structural
    from collections.abc import Callable

log = logging.getLogger(__name__)


# -- loop budgets: the kernel owns the defaults; an app installs its own -----------
class _Budgets(BaseModel):
    """The two budgets a :class:`BoundedLoop` reads. The kernel default keeps a loop
    usable with zero app config; the app (``webclient.settings``) registers a provider
    that returns its own, env-configured budgets instead (structurally its
    ``LoopSettings``)."""

    max_rounds: int = 20
    max_stalls: int = 3


_budget_provider: "Callable[[], Any] | None" = None


def set_loop_budget_provider(provider: "Callable[[], Any] | None") -> None:
    """Install (or clear, with ``None``) the source of default loop budgets -- any object
    with ``max_rounds`` / ``max_stalls`` fields. ``webclient.settings`` installs one so loops
    pick up the process-wide, env-configured budgets; the kernel imports nothing to do it
    (dependency inversion), so it never reaches up into the app config layer."""
    global _budget_provider
    _budget_provider = provider


def _loop_budgets() -> Any:
    """The effective default budgets: the installed provider's, else the kernel default."""
    return _budget_provider() if _budget_provider is not None else _Budgets()


S = TypeVar("S")  # the driven STATE (e.g. a live Document, a crawl, a query-in-progress)
O = TypeVar("O")  # an OBSERVATION handed to decide
D = TypeVar("D")  # a DECISION returned by decide


class Ask(BaseModel):
    """A driver's request for a HUMAN decision -- returned from ``decide`` instead of a
    decision. The loop checkpoints (``reason="waiting"``) and resumes with the answer.
    ``options`` are the choices offered (free-form when empty); ``detail`` is context for
    the UI (the observation summary, the candidate picks, ...)."""

    reason: str
    options: list[Any] = []
    detail: dict[str, Any] = {}


class LoopVerdict(BaseModel):
    """Why a :class:`BoundedLoop` stopped. ``reason``: ``done`` (the decision declared the loop
    finished), ``budget`` (hit ``max_rounds``), ``stalled`` (``max_stalls`` no-progress rounds in
    a row), ``error`` (``apply`` raised), or ``waiting`` (the driver asked for a human decision --
    see ``ask`` and :meth:`BoundedLoop.resume`). ``rounds`` is how many rounds ran; ``result`` is
    the done payload; ``error`` the failure message."""

    done: bool
    reason: Literal["done", "budget", "stalled", "error", "waiting"]
    rounds: int
    result: str = ""
    error: str = ""
    ask: Ask | None = None


@runtime_checkable
class Loop(Protocol):
    """What every loop in the package offers: a name, ``run`` (auto), ``step`` (manual, one
    round), ``resume`` (after an :class:`Ask`), the last ``verdict`` and the ``pending`` ask."""

    name: str

    @property
    def verdict(self) -> "LoopVerdict | None": ...
    @property
    def pending(self) -> "Ask | None": ...


class BoundedLoop(Generic[S, O, D]):
    """A reusable bounded loop over a mutating ``state``. Each round: ``observe`` the state,
    ``decide`` from the observation, and unless the decision is terminal, ``apply`` it -- until a
    decision is done, ``apply`` raises, progress stalls, the round budget is spent, or the driver
    asks for a human (``waiting``; continue with :meth:`resume`).

    Callables:
      * ``observe(state, round_index, last_error) -> O`` -- what the decider sees this round.
      * ``decide(observation) -> D | Ask`` -- the "brain" / DRIVER (an LLM adapter, a policy
        fn, ...); returning an :class:`Ask` checkpoints the loop for a human decision.
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
        decide: "Callable[[O], D | Ask]",
        done_result: "Callable[[D], str | None]",
        apply: "Callable[[S, D], None | Awaitable[None]]",  # sync, or async for a fetch-driven loop (astep/arun)
        progress: "Callable[[S], Any] | None" = None,
        max_rounds: "int | None" = None,
        max_stalls: "int | None" = None,
        fanout: "int | None" = None,
        name: str = "loop",
        bus: Any = None,
    ) -> None:
        budgets = _loop_budgets()
        self._observe = observe
        self._decide = decide
        self._done_result = done_result
        self._apply = apply
        self._progress = progress
        self.max_rounds = budgets.max_rounds if max_rounds is None else max_rounds
        self.max_stalls = budgets.max_stalls if max_stalls is None else max_stalls
        #: when set (async ``astep``/``arun`` only), a round's decision is a BATCH and ``apply`` is
        #: called per unit CONCURRENTLY, at most ``fanout`` in flight -- the loop owns the efficiency.
        self.fanout = fanout
        self.name = name
        self.bus = bus  # an EventBus to publish LoopEvents on (None = silent)
        # -- the checkpoint (set while waiting / between manual steps) ----------
        self.state: Any = _UNSET  # the checkpointed state (``_UNSET`` before the first step)
        self.round = 0  # rounds completed
        self.verdict: "LoopVerdict | None" = None
        self.pending: "Ask | None" = None
        self._stalls = 0
        self._prev: Any = _UNSET
        self._error = ""

    def _emit(self, phase: str, round_index: int, **detail: Any) -> None:
        """Publish a :class:`~webclient.models.LoopEvent` (a no-op without a bus)."""
        if self.bus is None:
            return
        from .models import LoopEvent

        self.bus.publish(LoopEvent(loop=self.name, phase=phase, round=round_index, detail=detail))  # type: ignore[arg-type]

    # -- one round (the manual primitive) --------------------------------------
    def step(self, state: S, decision: "D | Ask | None" = None) -> "LoopVerdict | None":
        """Run ONE round on ``state``: observe, then use ``decision`` (manual) or ask the driver
        (auto), then apply. Returns a :class:`LoopVerdict` when the round was terminal (done /
        error / stalled / budget / waiting), else ``None`` (keep stepping). The loop keeps its
        own round counter and stall state between steps, so ``step`` and ``run`` compose."""
        self.state = state
        i = self.round
        if i >= self.max_rounds:
            return self._finish(LoopVerdict(done=False, reason="budget", rounds=self.max_rounds))
        self._emit("round", i + 1, budget=self.max_rounds, error=self._error)
        if decision is None:
            decision = self._decide(self._observe(state, i, self._error))
            log.debug("%s round %d/%d: %r", self.name, i + 1, self.max_rounds, decision)
        if isinstance(decision, Ask):  # a checkpoint: hand the question up, keep the round
            self.pending = decision
            log.info("%s waiting at round %d: %s", self.name, i + 1, decision.reason)
            self._emit("waiting", i + 1, ask=decision.model_dump(mode="json"))
            return self._finish(LoopVerdict(done=False, reason="waiting", rounds=i, ask=decision), keep=True)
        self.pending = None
        self._emit("decision", i + 1, decision=_brief(decision))
        result = self._done_result(decision)
        if result is not None:
            log.info("%s done after %d round(s)", self.name, i)
            self._emit("done", i, result=result)
            return self._finish(LoopVerdict(done=True, reason="done", rounds=i, result=result))
        try:
            self._apply(state, decision)
            self._error = ""
        except Exception as exc:  # noqa: BLE001 - surface it to the verdict, never crash
            log.warning("%s stopped on error at round %d: %s", self.name, i + 1, exc)
            self._emit("error", i + 1, error=str(exc))
            return self._finish(LoopVerdict(done=False, reason="error", rounds=i, error=str(exc)))
        self.round = i + 1
        if self._progress is not None:
            current = self._progress(state)
            self._stalls = self._stalls + 1 if current == self._prev else 0
            self._prev = current
            if self._stalls >= self.max_stalls:
                log.info("%s stalled after %d round(s)", self.name, i + 1)
                self._emit("stalled", i + 1)
                return self._finish(LoopVerdict(done=False, reason="stalled", rounds=i + 1))
        if self.round >= self.max_rounds:
            log.info("%s hit its round budget (%d)", self.name, self.max_rounds)
            self._emit("budget", self.max_rounds)
            return self._finish(LoopVerdict(done=False, reason="budget", rounds=self.max_rounds))
        return None

    def _finish(self, verdict: LoopVerdict, *, keep: bool = False) -> LoopVerdict:
        self.verdict = verdict
        if not keep:
            self.pending = None
        return verdict

    # -- auto ----------------------------------------------------------------------
    def run(self, state: S) -> LoopVerdict:
        """Drive ``state`` round by round until a terminal condition, returning the verdict.
        Terminates on: a done decision (``done``), an ``apply`` exception (``error``, captured
        never raised), ``max_stalls`` consecutive no-progress rounds (``stalled``),
        ``max_rounds`` (``budget``), or the driver asking for a human (``waiting`` -- call
        :meth:`resume` with the answer to continue)."""
        self.state = state
        self.round = 0
        self._stalls = 0
        self._prev = _UNSET
        self._error = ""
        self.pending = None
        return self._drive(state)

    def _drive(self, state: S, first: "D | Ask | None" = None) -> LoopVerdict:
        verdict = self.step(state, first)
        while verdict is None:
            verdict = self.step(state)
        return verdict

    def resume(self, decision: "D | Ask") -> LoopVerdict:
        """Continue a ``waiting`` loop from its checkpoint with the human's ``decision`` for
        the round that asked (then keep driving). Raises if the loop is not waiting."""
        if self.pending is None or self.state is _UNSET:
            raise RuntimeError(f"{self.name} is not waiting for a decision")
        self._emit("resumed", self.round + 1, decision=_brief(decision))
        self.pending = None
        return self._drive(cast("S", self.state), decision)

    # -- async twins (for loops whose apply/observe/decide are async, e.g. a fetch) --------------
    async def astep(self, state: S, decision: "D | Ask | None" = None) -> "LoopVerdict | None":
        """The async twin of :meth:`step`: one round where ``observe`` / ``decide`` / ``apply`` /
        ``progress`` may be async (a coroutine result is awaited; a plain value is used as is). Same
        rounds, stall detection and verdicts as :meth:`step`, so a fetch-driven loop (crawl,
        pagination) is a BoundedLoop like every other -- it just runs on the caller's event loop."""
        self.state = state
        i = self.round
        if i >= self.max_rounds:
            return self._finish(LoopVerdict(done=False, reason="budget", rounds=self.max_rounds))
        self._emit("round", i + 1, budget=self.max_rounds, error=self._error)
        if decision is None:
            obs = await _aw(self._observe(state, i, self._error))
            decision = cast("D | Ask", await _aw(self._decide(obs)))
            log.debug("%s round %d/%d: %r", self.name, i + 1, self.max_rounds, decision)
        if isinstance(decision, Ask):  # a checkpoint: hand the question up, keep the round
            self.pending = decision
            self._emit("waiting", i + 1, ask=decision.model_dump(mode="json"))
            return self._finish(LoopVerdict(done=False, reason="waiting", rounds=i, ask=decision), keep=True)
        self.pending = None
        self._emit("decision", i + 1, decision=_brief(decision))
        result = self._done_result(decision)
        if result is not None:
            self._emit("done", i, result=result)
            return self._finish(LoopVerdict(done=True, reason="done", rounds=i, result=result))
        try:
            if self.fanout is not None:  # a BATCH round: apply each unit concurrently (bounded)
                await _fan_out(decision, lambda unit: self._apply(state, unit), self.fanout)
            else:
                await _aw(self._apply(state, decision))
            self._error = ""
        except Exception as exc:  # noqa: BLE001 - surface it to the verdict, never crash
            log.warning("%s stopped on error at round %d: %s", self.name, i + 1, exc)
            self._emit("error", i + 1, error=str(exc))
            return self._finish(LoopVerdict(done=False, reason="error", rounds=i, error=str(exc)))
        self.round = i + 1
        if self._progress is not None:
            current = await _aw(self._progress(state))
            self._stalls = self._stalls + 1 if current == self._prev else 0
            self._prev = current
            if self._stalls >= self.max_stalls:
                self._emit("stalled", i + 1)
                return self._finish(LoopVerdict(done=False, reason="stalled", rounds=i + 1))
        if self.round >= self.max_rounds:
            self._emit("budget", self.max_rounds)
            return self._finish(LoopVerdict(done=False, reason="budget", rounds=self.max_rounds))
        return None

    async def arun(self, state: S) -> LoopVerdict:
        """The async twin of :meth:`run`: drive ``state`` round by round on the caller's loop until a
        terminal condition, returning the verdict (same taxonomy as :meth:`run`)."""
        self.state = state
        self.round = 0
        self._stalls = 0
        self._prev = _UNSET
        self._error = ""
        self.pending = None
        verdict = await self.astep(state)
        while verdict is None:
            verdict = await self.astep(state)
        return verdict


async def _aw(value: Any) -> Any:
    """Await ``value`` when it is awaitable (an async callable's result), else return it as is --
    so :meth:`BoundedLoop.astep` accepts async OR sync ``observe``/``decide``/``apply``/``progress``."""
    if inspect.isawaitable(value):
        return await value
    return value


async def _fan_out(units: Any, fn: "Callable[[Any], Any]", limit: int) -> None:
    """Apply ``fn`` to each unit of a round's batch CONCURRENTLY, at most ``limit`` in flight -- the
    efficiency primitive a batch loop (a crawl round fetching several edges) drives its work with,
    so concurrency lives in the loop, not hand-rolled around it. A unit's ``fn`` may be sync or async."""
    items = list(units)
    if not items:
        return
    sem = asyncio.Semaphore(max(1, limit))

    async def run(unit: Any) -> None:
        async with sem:
            await _aw(fn(unit))

    await asyncio.gather(*(run(unit) for unit in items))


_UNSET: Any = object()  # a first-round sentinel that no progress signature can equal


def _brief(decision: Any) -> Any:
    """A JSON-friendly summary of a decision for the LoopEvent (a model's dump, else its repr)."""
    dump = getattr(decision, "model_dump", None)
    if dump is not None:
        try:
            return dump(mode="json")
        except Exception:  # noqa: BLE001
            pass
    return repr(decision)[:200]


__all__ = ["Ask", "BoundedLoop", "Loop", "LoopVerdict"]
