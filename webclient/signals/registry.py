"""The detector registry: the one place detection is wired.

A **detector** is a small function that reads a :class:`Context` and returns a
:class:`Hit` (its confidence + evidence) or ``None``. It is registered against a
flag with a stage, via the :func:`detector` decorator. A **flag** is registered with
:func:`flag`, declaring how to derive its ``remedy`` and ``value`` from the signals
that fired.

To add a signal: write a ``@detector(flag=..., name=..., stage=...)`` function.
To add a flag: ``@flag(name=..., remedy=...)`` plus one or more detectors for it.
Nothing else changes -- ``run`` / ``flags`` iterate the registries. Extension is
purely additive, and detection stays isolated from the cores.

``Signal`` / ``Flag`` are imported lazily (runtime only) to avoid an import cycle
with ``core.document``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, NamedTuple, cast

from .context import Context

if TYPE_CHECKING:
    from ..core.document.models import Flag, Signal, Stage

_PRESENT = 0.5  # a flag's confidence must reach this to be "present"

#: a detector's finding: its confidence (0-1) plus the evidence it read.
class Hit(NamedTuple):
    confidence: float
    reason: str = ""
    value: Any = None


DetectorFn = Callable[[Context], "Hit | None"]
#: how a flag derives its remedy / value from the signals that fired (+ the context).
RemedyFn = Callable[["list[Signal]", Context], "str | None"]
ValueFn = Callable[["list[Signal]", Context], Any]


@dataclass(frozen=True)
class Detector:
    flag: str
    name: str
    stage: str
    fn: DetectorFn


@dataclass
class FlagSpec:
    name: str
    remedy: "str | RemedyFn | None" = None
    value: "ValueFn | None" = None


DETECTORS: list[Detector] = []
FLAGS: dict[str, FlagSpec] = {}


def detector(*, flag: str, name: str, stage: "Stage") -> Callable[[DetectorFn], DetectorFn]:
    """Register ``fn`` as a detector feeding ``flag`` from ``stage``. ``fn(ctx)``
    returns a :class:`Hit` when the evidence is present, else ``None``."""
    def wrap(fn: DetectorFn) -> DetectorFn:
        DETECTORS.append(Detector(flag=flag, name=name, stage=stage, fn=fn))
        return fn
    return wrap


def flag(name: str, *, remedy: "str | RemedyFn | None" = None, value: "ValueFn | None" = None) -> FlagSpec:
    """Register a flag and how it derives its remedy / value. Returns the spec so a
    caller can keep a reference; idempotent per name (a re-register replaces)."""
    spec = FlagSpec(name=name, remedy=remedy, value=value)
    FLAGS[name] = spec
    return spec


def _combine(confidences: "list[float]") -> float:
    """Noisy-OR: corroborating evidence raises the total. 1 - Π(1-c)."""
    p = 1.0
    for c in confidences:
        p *= 1.0 - min(1.0, max(0.0, c))
    return round(1.0 - p, 3)


def build_flag(name: str, signals: "list[Signal]", *, remedy: str | None = None, value: Any = None) -> "Flag":
    """Roll signals up into a flag: confidence = noisy-OR; ``present`` at the
    threshold; ``remedy`` applies only once present."""
    from ..core.document.models import Flag

    conf = _combine([s.confidence for s in signals])
    present = conf >= _PRESENT
    return Flag(
        name=name, present=present, confidence=conf, signals=list(signals),
        remedy=cast(Any, remedy) if present else None, value=value,
    )


def run(ctx: Context) -> "list[Signal]":
    """Every signal that fires for ``ctx`` -- run each registered detector; a
    detector whose inputs are absent (e.g. a rendered detector with no browser
    events) simply returns ``None``."""
    from ..core.document.models import Signal

    out: list[Signal] = []
    for d in DETECTORS:
        hit = d.fn(ctx)
        if hit is not None and hit.confidence > 0.0:
            out.append(Signal(
                name=d.name, flag=d.flag, stage=cast(Any, d.stage),
                confidence=hit.confidence, reason=hit.reason, value=hit.value,
            ))
    return out


def flags(ctx: Context) -> "dict[str, Flag]":
    """Every registered flag, built from the signals that fired for ``ctx``. A flag
    with no evidence is present=False (a total surface -- never an error)."""
    signals = run(ctx)
    by: dict[str, list[Any]] = {}
    for s in signals:
        by.setdefault(s.flag, []).append(s)
    out: dict[str, Any] = {}
    for name, spec in FLAGS.items():
        group = by.get(name, [])
        remedy = spec.remedy(group, ctx) if callable(spec.remedy) else spec.remedy
        value = spec.value(group, ctx) if spec.value is not None else None
        out[name] = build_flag(name, group, remedy=remedy, value=value)
    return out


__all__ = [
    "Context", "Hit", "Detector", "FlagSpec",
    "detector", "flag", "build_flag", "run", "flags",
    "DETECTORS", "FLAGS",
]
