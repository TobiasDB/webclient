"""Flags -- the CONCLUSION surface over :mod:`.signals`.

A signal is a piece of evidence ("a password field is present", confidence 1.0). A **flag** is the
conclusion a caller acts on ("auth_required", and here is the remedy). :func:`flags` runs the
detectors over a resolved page (its :class:`~web.parse.Document` and the transport
:class:`~web.fetch.Snapshot`), rolls the supporting evidence up per conclusion by **noisy-OR**
(independent evidence reinforces; a strong contra pulls a conclusion back down), and returns the
conclusions that fire -- each with a ``remedy`` string the auto-resolver / pipeline dispatches on.

This is the detection substrate the memory calls "Flags (conclusions) built from tiered,
confidence-scored Signals (evidence)". Adding a conclusion = one entry in :data:`_CONCLUSIONS`;
adding evidence = a signal function referenced from an entry.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import BaseModel, JsonValue
from web.fetch import Snapshot
from web.parse import Document

from . import signals as _s
from .signals import Signal

#: a detector reads either the parsed content (``"doc"``) or the transport snapshot (``"snap"``).
_DocDetector = Callable[[Document], "Signal | None"]
_SnapDetector = Callable[[Snapshot], "Signal | None"]


class Flag(BaseModel):
    """A conclusion about a resolved page: whether it holds, the combined confidence, a human
    ``description`` of what it means, the evidence that fired, and the ``remedy`` a caller can act on
    (``escalate:browser``, ``retry``, …).
    """

    name: str
    present: bool
    confidence: float
    description: str = ""
    remedy: "str | None" = None
    signals: list[Signal] = []
    detail: dict[str, JsonValue] = {}


@dataclass(frozen=True)
class _Conclusion:
    name: str
    remedy: "str | None"
    description: str = ""
    doc_detectors: tuple[_DocDetector, ...] = ()
    snap_detectors: tuple[_SnapDetector, ...] = ()
    #: contra evidence -- detectors whose hit REDUCES this conclusion's confidence.
    contra: tuple[_DocDetector, ...] = field(default_factory=tuple)


#: the conclusion table -- each rolls up its evidence into one actionable flag + remedy.
_CONCLUSIONS: tuple[_Conclusion, ...] = (
    _Conclusion(
        "needs_browser",
        "escalate:browser",
        "JS-gated / near-empty static HTML -- render in a browser",
        doc_detectors=(_s.spa, _s.empty),
    ),
    _Conclusion(
        "blocked",
        "escalate:proxy",
        "an anti-bot wall or a blocking status (401/403/429) -- NOT the dataset",
        doc_detectors=(_s.anti_bot,),
        snap_detectors=(_s.blocked_status,),
    ),
    _Conclusion(
        "auth_required",
        "session:login",
        "a login wall stands between the crawler and the data",
        doc_detectors=(_s.login_wall,),
    ),
    _Conclusion(
        "consent_wall",
        "dismiss:consent",
        "a cookie/consent interstitial to dismiss first",
        doc_detectors=(_s.consent_wall,),
    ),
    _Conclusion(
        "paginated",
        "paginate",
        "the dataset spans multiple pages (a pager)",
        doc_detectors=(_s.pagination,),
    ),
    _Conclusion(
        "infinite_scroll",
        "paginate:scroll",
        "more rows load on scroll (infinite scroll)",
        doc_detectors=(_s.infinite_scroll,),
    ),
    _Conclusion(
        "server_error",
        "retry",
        "the server errored (5xx) -- transient, retry",
        snap_detectors=(_s.server_error,),
    ),
    _Conclusion(
        "structured_data",
        "extract:jsonld",
        "machine-readable JSON-LD / microdata about the page",
        doc_detectors=(_s.structured_data,),
    ),
    _Conclusion(
        "data_api",
        "extract:json_island",
        "a JSON data-island / API backs the page",
        doc_detectors=(_s.data_api,),
    ),
    _Conclusion(
        "record_list",
        "extract:records",
        "a repeating record region -- a likely dataset to extract",
        doc_detectors=(_s.record_list,),
    ),
    _Conclusion(
        "tabbed",
        "interact:tabs",
        "the data is split across tabs (interaction needed)",
        doc_detectors=(_s.tabbed,),
    ),
    _Conclusion(
        "iframe",
        "descend:iframe",
        "the data lives in an embedded iframe",
        doc_detectors=(_s.iframe,),
    ),
)


def _noisy_or(confidences: "list[float]") -> float:
    """Combine independent positive evidence: ``1 - Π(1 - c)`` -- each hit only raises the total."""
    p = 1.0
    for c in confidences:
        p *= 1.0 - min(1.0, max(0.0, c))
    return 1.0 - p


def _collect(concl: _Conclusion, doc: Document, snap: "Snapshot | None") -> "list[Signal]":
    hits: list[Signal] = []
    for d in concl.doc_detectors:
        if (sig := d(doc)) is not None:
            hits.append(sig)
    if snap is not None:
        for sd in concl.snap_detectors:
            if (sig := sd(snap)) is not None:
                hits.append(sig)
    return hits


def flag_for(
    concl: _Conclusion, doc: Document, snap: "Snapshot | None", *, threshold: float
) -> "Flag | None":
    positive = _collect(concl, doc, snap)
    if not positive:
        return None
    conf = _noisy_or([s.confidence for s in positive])
    for c in concl.contra:  # contra evidence pulls the confidence down
        if (sig := c(doc)) is not None:
            conf *= 1.0 - min(1.0, max(0.0, sig.confidence))
            positive.append(sig)
    return Flag(
        name=concl.name,
        present=conf >= threshold,
        confidence=round(conf, 4),
        description=concl.description,
        remedy=concl.remedy,
        signals=positive,
    )


def flags(
    doc: Document, snapshot: "Snapshot | None" = None, *, threshold: float = 0.5
) -> "list[Flag]":
    """Every conclusion that FIRES for a resolved page (``present`` at/above ``threshold``), each
    with its combined confidence, supporting signals, and remedy -- ordered most-confident first.
    Pass the ``snapshot`` too for the transport-level conclusions (blocked-by-status, server error).
    """
    out: list[Flag] = []
    for concl in _CONCLUSIONS:
        f = flag_for(concl, doc, snapshot, threshold=threshold)
        if f is not None and f.present:
            out.append(f)
    out.sort(key=lambda f: f.confidence, reverse=True)
    return out


__all__ = ["Flag", "flags", "flag_for"]
