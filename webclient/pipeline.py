"""Pipelines (roadmap N12): an ordered set of STAGES with gates and reviews, run over a
shared context, publishing a :class:`~webclient.models.PipelineEvent` at every boundary so
a trace / UI can draw the run -- and checkpointing on an :class:`~webclient.loop.Ask` so a
human can decide at a gate (the same interrupt/resume primitive the loops use).

    pipe = Pipeline("onboarding", [
        Stage("search", run=search),                       # ctx -> output (stored on ctx)
        Stage("crawl", run=crawl, gate=lambda ctx, out: bool(out.pages) or "no pages"),
        Stage("query", run=author, review=grade),          # a review is a FLAG, never a gate
    ], bus=wc.bus)
    run = pipe.run(ctx)                 # PipelineRun: ok / stopped_at / reason / outputs
    if run.waiting: pipe.resume(answer) # after a stage returned an Ask

A stage that IS a loop (a crawl, a query loop) simply runs it inside ``run`` -- its
LoopEvents nest under the stage's PipelineEvents by time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel

from .loop import Ask

log = logging.getLogger(__name__)

__all__ = ["Stage", "Pipeline", "PipelineRun"]


@dataclass
class Stage:
    """One stage. ``run(ctx)`` produces the stage's output (stored on ``ctx`` under ``name``
    and in the run's ``outputs``); it may return an :class:`Ask` to checkpoint. ``gate(ctx,
    out)`` returns ``True`` to continue or a reason string to STOP the pipeline (a stage
    marked ``optional`` only records the reason and continues). ``review(ctx, out)`` is a
    non-gating assessment recorded on the run (a flag for a human)."""

    name: str
    run: Callable[[Any], Any]
    gate: "Callable[[Any, Any], bool | str] | None" = None
    review: "Callable[[Any, Any], Any] | None" = None
    optional: bool = False
    description: str = ""
    #: a summary of the stage's OUTPUT for the live event -- ``detail(ctx, out) -> dict`` -- so a
    #: watcher (a UI, a trace) sees WHAT each stage produced the moment it finishes, not just that
    #: it did. Kept small + JSON-safe (urls, counts, key fields); rides the ``exit`` event.
    detail: "Callable[[Any, Any], dict[str, Any]] | None" = None


class PipelineRun(BaseModel):
    """The outcome: whether every gate passed, where it stopped and why, each stage's
    output, the reviews, and the pending :class:`Ask` when a stage is waiting."""

    pipeline: str = ""
    ok: bool = False
    stopped_at: str = ""
    reason: str = ""
    completed: list[str] = []
    outputs: dict[str, Any] = {}
    reviews: dict[str, Any] = {}
    gates: dict[str, str] = {}  # stage -> the gate's reason when it did not pass
    waiting: bool = False
    ask: Ask | None = None


class Pipeline:
    """An ordered set of stages over one context object (anything: a dataclass, a dict, a
    result model the stages fill in)."""

    def __init__(
        self, name: str, stages: "list[Stage]", *, bus: Any = None,
        propagate: "tuple[type[BaseException], ...]" = (),
    ) -> None:
        self.name = name
        self.stages = list(stages)
        self.bus = bus
        #: exception types a stage may raise THROUGH the pipeline (a budget cap, a cancel)
        #: instead of being recorded as the run's reason.
        self.propagate = propagate
        self.run_state: PipelineRun = PipelineRun(pipeline=name)
        self._ctx: Any = None
        self._index = 0  # the next stage to run

    @property
    def pending(self) -> "Ask | None":
        return self.run_state.ask if self.run_state.waiting else None

    def _emit(self, stage: str, phase: str, **detail: Any) -> None:
        if self.bus is None:
            return
        from .kernel.models import PipelineEvent

        self.bus.publish(PipelineEvent(pipeline=self.name, stage=stage, phase=phase, detail=detail))  # type: ignore[arg-type]

    def _stage_detail(self, stage: "Stage", ctx: Any, out: Any) -> "dict[str, Any]":
        """The stage's OUTPUT summary for its exit event (empty when it has no ``detail`` hook or
        the hook raises -- a summary must never break the run)."""
        if stage.detail is None:
            return {}
        try:
            return stage.detail(ctx, out) or {}
        except Exception:  # noqa: BLE001
            return {}

    def run(self, ctx: Any) -> PipelineRun:
        """Run every stage in order over ``ctx``. Stops at the first failing gate (unless the
        stage is ``optional``), a stage exception (recorded as ``reason``), or an :class:`Ask`
        (``waiting``); ``ok`` is True only when every stage ran and every gate passed."""
        self._ctx = ctx
        self._index = 0
        self.run_state = PipelineRun(pipeline=self.name)
        return self._advance(first_answer=None)

    def resume(self, answer: Any) -> PipelineRun:
        """Continue a waiting pipeline: the waiting stage is re-run with ``answer`` available
        as ``ctx.answer`` (set on the context, or under the ``"answer"`` key of a dict)."""
        if not self.run_state.waiting:
            raise RuntimeError(f"pipeline {self.name!r} is not waiting")
        self.run_state.waiting = False
        self.run_state.ask = None
        self._emit(self.stages[self._index].name, "enter", resumed=True)
        return self._advance(first_answer=answer)

    def _advance(self, *, first_answer: Any) -> PipelineRun:
        ctx, run = self._ctx, self.run_state
        answer = first_answer
        while self._index < len(self.stages):
            stage = self.stages[self._index]
            if answer is not None:
                _set(ctx, "answer", answer)
                answer = None
            else:
                self._emit(stage.name, "enter")
            try:
                out = stage.run(ctx)
            except self.propagate:
                self._emit(stage.name, "error", error="propagated")
                raise
            except Exception as exc:  # noqa: BLE001 - a stage failure ends the run, recorded
                run.stopped_at, run.reason = stage.name, f"{type(exc).__name__}: {exc}"
                log.warning("pipeline %s: stage %s failed: %s", self.name, stage.name, exc)
                self._emit(stage.name, "error", error=run.reason)
                return run
            if isinstance(out, Ask):
                run.waiting, run.ask = True, out
                self._emit(stage.name, "gate", waiting=True, ask=out.model_dump(mode="json"))
                return run
            run.outputs[stage.name] = out
            _set(ctx, stage.name, out)
            if stage.review is not None:
                try:
                    review = stage.review(ctx, out)
                except Exception as exc:  # noqa: BLE001 - a review never breaks the run
                    review = f"review failed: {exc}"
                if review is not None:
                    run.reviews[stage.name] = review
                    self._emit(stage.name, "review", review=_jsonish(review))
            if stage.gate is not None:
                verdict = stage.gate(ctx, out)
                if verdict is not True:
                    reason = str(verdict) if verdict else f"{stage.name}: gate failed"
                    run.gates[stage.name] = reason
                    self._emit(stage.name, "gate", passed=False, reason=reason, optional=stage.optional)
                    if not stage.optional:
                        run.stopped_at, run.reason = stage.name, reason
                        self._emit(stage.name, "exit", stopped=True, **self._stage_detail(stage, ctx, out))
                        return run
                else:
                    self._emit(stage.name, "gate", passed=True)
            run.completed.append(stage.name)
            self._emit(stage.name, "exit", **self._stage_detail(stage, ctx, out))  # the stage's output, for the live watcher
            self._index += 1
        run.ok = True
        return run


def _set(ctx: Any, key: str, value: Any) -> None:
    if isinstance(ctx, dict):
        ctx[key] = value
        return
    try:
        setattr(ctx, key, value)
    except Exception:  # noqa: BLE001 - a frozen/strict context keeps outputs on the run only
        pass


def _jsonish(value: Any) -> Any:
    dump = getattr(value, "model_dump", None)
    if dump is not None:
        try:
            return dump(mode="json")
        except Exception:  # noqa: BLE001
            pass
    return value if isinstance(value, (str, int, float, bool, list, dict)) or value is None else str(value)
