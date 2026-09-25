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
    contra: bool = False  # CONTRA evidence: its Hit REDUCES the flag instead of raising it
    #: the page scripts (by registry name) whose data this detector reads -- a rendered /
    #: network detector needs the browser observer; a request / static one needs nothing.
    needs: tuple[str, ...] = ()


@dataclass
class FlagSpec:
    name: str
    remedy: "str | RemedyFn | None" = None
    value: "ValueFn | None" = None
    #: which flag GROUP this is. ``"conclusion"`` (default) -- the notable conclusions auto/resolve
    #: act on (spa / login / pagination / ...); ``"pattern"`` -- structural recon (record regions,
    #: repeated controls, the page template) that is ALWAYS-present detail, kept out of the conclusion
    #: set so it never pollutes "any flag present?" logic. ``flags(ctx)`` builds one group at a time.
    group: str = "conclusion"


DETECTORS: list[Detector] = []
FLAGS: dict[str, FlagSpec] = {}


def detector(
    *, flag: str, name: str, stage: "Stage", contra: bool = False, needs: "tuple[str, ...] | None" = None
) -> Callable[[DetectorFn], DetectorFn]:
    """Register ``fn`` as a detector feeding ``flag`` from ``stage``. ``fn(ctx)``
    returns a :class:`Hit` when the evidence is present, else ``None``. ``contra=True``
    makes it CONTRA evidence -- its Hit lowers the flag's confidence (e.g. "the dataset is
    already in the served HTML" pulling ``spa`` down) rather than raising it. ``needs``
    names the page scripts whose data it reads (defaults to the browser observer for a
    rendered / network stage, nothing for request / static)."""
    if needs is None:
        needs = ("LiveBacking.init", "LiveBacking.drain") if stage in ("rendered", "network") else ()

    def wrap(fn: DetectorFn) -> DetectorFn:
        DETECTORS.append(Detector(flag=flag, name=name, stage=stage, fn=fn, contra=contra, needs=needs))
        return fn
    return wrap


def flag(
    name: str, *, remedy: "str | RemedyFn | None" = None, value: "ValueFn | None" = None,
    group: str = "conclusion",
) -> FlagSpec:
    """Register a flag and how it derives its remedy / value. ``group`` separates the notable
    conclusions (``"conclusion"``, the default -- what auto/resolve act on) from structural
    ``"pattern"`` recon, so the always-present pattern flags never pollute the conclusion set.
    Returns the spec; idempotent per name (a re-register replaces)."""
    spec = FlagSpec(name=name, remedy=remedy, value=value, group=group)
    FLAGS[name] = spec
    return spec


def _combine(confidences: "list[float]") -> float:
    """Noisy-OR: corroborating evidence raises the total. 1 - Π(1-c)."""
    p = 1.0
    for c in confidences:
        p *= 1.0 - min(1.0, max(0.0, c))
    return round(1.0 - p, 3)


def build_flag(name: str, signals: "list[Signal]", *, remedy: str | None = None, value: Any = None) -> "Flag":
    """Roll signals up into a flag: POSITIVE evidence combines by noisy-OR; each CONTRA
    signal then multiplies the confidence DOWN by ``(1 - its confidence)`` (so a strong
    contra can pull a flag below the present threshold). ``present`` at the threshold;
    ``remedy`` applies only once present."""
    from ..core.document.models import Flag

    conf = _combine([s.confidence for s in signals if not s.contra])
    for s in signals:  # contra evidence reduces the confidence
        if s.contra:
            conf *= 1.0 - min(1.0, max(0.0, s.confidence))
    from ..settings import current

    conf = round(conf, 3)
    present = conf >= current().detection.present_threshold
    return Flag(
        name=name, present=present, confidence=conf, signals=list(signals),
        remedy=cast(Any, remedy) if present else None, value=value,
    )


def run(ctx: Context, wanted: "set[str] | None" = None) -> "list[Signal]":
    """Every signal that fires for ``ctx`` -- run each registered detector; a detector whose inputs
    are absent (e.g. a rendered detector with no browser events) simply returns ``None``. ``wanted``
    restricts it to detectors feeding those flags (so building one group doesn't run the others)."""
    from ..core.document.models import Signal

    out: list[Signal] = []
    for d in DETECTORS:
        if wanted is not None and d.flag not in wanted:
            continue
        hit = d.fn(ctx)
        if hit is not None and hit.confidence > 0.0:
            out.append(Signal(
                name=d.name, flag=d.flag, stage=cast(Any, d.stage),
                confidence=hit.confidence, contra=d.contra, reason=hit.reason, value=hit.value,
            ))
    return out


def flags(ctx: Context, *, group: str = "conclusion") -> "dict[str, Flag]":
    """The flags of one ``group`` built from the signals that fired for ``ctx`` -- ``"conclusion"``
    (the default: the notable conclusions auto/resolve act on) or ``"pattern"`` (structural recon).
    A flag with no evidence is present=False (a total surface -- never an error). Building one group
    runs only that group's detectors, so the always-present pattern flags never enter the conclusion
    set."""
    wanted = {name for name, spec in FLAGS.items() if spec.group == group}
    signals = run(ctx, wanted)
    by: dict[str, list[Signal]] = {}
    for s in signals:
        by.setdefault(s.flag, []).append(s)
    out: dict[str, Flag] = {}
    for name in wanted:
        spec = FLAGS[name]
        group_signals = by.get(name, [])
        remedy = spec.remedy(group_signals, ctx) if callable(spec.remedy) else spec.remedy
        value = spec.value(group_signals, ctx) if spec.value is not None else None
        out[name] = build_flag(name, group_signals, remedy=remedy, value=value)
    return out


__all__ = [
    "Context", "Hit", "Detector", "FlagSpec",
    "detector", "flag", "build_flag", "run", "flags",
    "DETECTORS", "FLAGS",
]
