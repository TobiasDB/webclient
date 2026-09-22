"""Web patterns (roadmap N11, PoC): detect RECURRING STRUCTURE in a page and emit hints that
extraction, interaction and crawl consume -- each in its own sense of the same fact.

Four recon families are foreseen; this PoC ships **DOM recon** only, built on the pure
toolkit that already exists (:mod:`webclient.dom.records` for the MDR-style repeating
region, :mod:`webclient.dom.skeleton` for structural signatures). Visual (screenshot) and
behaviour (event-stream) recon are deferred; the trace captures both inputs so the data is
there when wanted. Fingerprint recon reuses the state fingerprint of
:mod:`webclient.core.document.fingerprint`.

Patterns are REGISTERED like signals -- ``@pattern(name, kind, for_=(...))`` over a
:class:`PatternContext`, returning :class:`PatternHint` s -- so adding one is one function.
A hint says WHAT repeats (``subject``: a selector or a signature), HOW MUCH (``count``),
how sure (``confidence``), and WHO it is for: ``extract`` (the record list to
``select_all``), ``interact`` (a repeated control -- one action per item), ``crawl``
(a page-template signature -- pages sharing it are the same kind of page).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal

from pydantic import BaseModel

__all__ = ["PatternHint", "PatternContext", "pattern", "PATTERNS", "detect", "template_signature"]

Kind = Literal["dom", "visual", "behavior", "fingerprint"]
For = Literal["extract", "interact", "crawl"]


class PatternHint(BaseModel):
    """One detected pattern and the hint it carries for its consumers."""

    name: str  # the pattern's registry name ("record_list" / "repeated_control" / ...)
    kind: Kind = "dom"
    subject: str = ""  # a selector (extract / interact) or a signature (crawl)
    count: int = 0  # how many repeats
    confidence: float = 0.0  # 0-1
    for_: tuple[For, ...] = ()  # the consumers this hint is meant for
    evidence: str = ""  # a human-readable reason
    value: Any = None  # a consumer-specific payload (e.g. sample labels)


@dataclass(frozen=True)
class PatternContext:
    """What a pattern detector may read: the parsed tree, the page's URL and (when a browser
    captured them) its events. Pure; built by the facet."""

    tree: Any = None
    url: str = ""
    events: list[Any] = field(default_factory=list)


PatternFn = Callable[[PatternContext], "Iterable[PatternHint]"]


@dataclass(frozen=True)
class Pattern:
    name: str
    kind: Kind
    for_: tuple[For, ...]
    fn: PatternFn


PATTERNS: list[Pattern] = []


def pattern(name: str, *, kind: Kind = "dom", for_: "tuple[For, ...]" = ("extract",)) -> Callable[[PatternFn], PatternFn]:
    """Register a pattern detector: ``fn(ctx)`` yields :class:`PatternHint` s (or nothing)."""
    def wrap(fn: PatternFn) -> PatternFn:
        PATTERNS.append(Pattern(name=name, kind=kind, for_=for_, fn=fn))
        return fn
    return wrap


def detect(ctx: PatternContext, *, for_: "For | None" = None) -> "list[PatternHint]":
    """Every hint the registered detectors find for ``ctx`` (optionally only those meant
    for one consumer), most confident first. A detector that raises is skipped -- a hint is
    advisory, never a failure."""
    out: list[PatternHint] = []
    for p in PATTERNS:
        if for_ is not None and for_ not in p.for_:
            continue
        try:
            for hint in p.fn(ctx):
                if not hint.for_:
                    hint = hint.model_copy(update={"for_": p.for_})
                if not hint.name:
                    hint = hint.model_copy(update={"name": p.name})
                out.append(hint)
        except Exception:  # noqa: BLE001 - advisory
            continue
    out.sort(key=lambda h: h.confidence, reverse=True)
    return out


# --------------------------------------------------------------------------- #
# DOM recon
# --------------------------------------------------------------------------- #


def _pure_tree(ctx: PatternContext) -> Any:
    return ctx.tree if ctx.tree is not None and isinstance(getattr(ctx.tree, "tag", None), str) else None


@pattern("record_list", for_=("extract",))
def _record_list(ctx: PatternContext) -> "Iterable[PatternHint]":
    """The repeating dataset regions (the MDR-style dominant sibling groups): each is a
    ``select_all`` target for extraction. Confidence grows with the group size and content
    richness relative to the best region."""
    from ..dom.records import find_record_regions

    root = _pure_tree(ctx)
    if root is None:
        return []
    regions = find_record_regions(root, top_k=3)
    if not regions:
        return []
    best = regions[0].score or 1.0
    return [
        PatternHint(
            name="record_list", subject=r.item_selector, count=r.count,
            confidence=round(min(1.0, 0.5 + 0.5 * (r.score / best)), 3),
            evidence=f"{r.count} structurally identical siblings under <{r.container_tag}"
                     + (f"#{r.container_id}" if r.container_id else "") + ">",
        )
        for r in regions
    ]


@pattern("repeated_control", for_=("interact",))
def _repeated_control(ctx: PatternContext) -> "Iterable[PatternHint]":
    """Interactive controls that repeat with the same structure and label shape (an "add to
    cart" per card, a "load more" per section): one action per item. The subject is a durable
    selector for the first instance; ``count`` is how many share it."""
    from ..dom.index import durable_selector
    from ..dom.interactivity import interactive
    from ..dom.naming import name as _name
    from ..dom.skeleton import selector_sig

    root = _pure_tree(ctx)
    if root is None:
        return []
    groups: dict[tuple[str, str], list[Any]] = {}
    for el in root.iter():
        if not isinstance(getattr(el, "tag", None), str) or interactive(el) is None:
            continue
        named = _name(el)
        key = (selector_sig(el), (named.label if named else "").lower())
        groups.setdefault(key, []).append(el)
    out: list[PatternHint] = []
    for (sig, label), els in groups.items():
        if len(els) < 3 or not label:
            continue
        out.append(PatternHint(
            name="repeated_control", subject=durable_selector(els[0]), count=len(els),
            confidence=round(min(1.0, 0.4 + 0.1 * len(els)), 3),
            evidence=f'{len(els)} × {sig} labelled "{label}"', value={"label": label},
        ))
    return out


def template_signature(root: Any, *, budget: int = 3) -> str:
    """A page-TEMPLATE signature: the structural signature of the body's top levels (tags,
    ids, semantic classes -- no text), so pages built from the same template share it and a
    crawl can treat them as one kind of page. Bounded depth keeps it stable across data."""
    from ..dom.skeleton import kept_children, selector_sig

    def walk(el: Any, depth: int) -> str:
        kids = kept_children(el)
        inner = "" if depth >= budget else ",".join(walk(k, depth + 1) for k in kids[:12])
        return f"{selector_sig(el)}({inner})"

    body = root.find("body") if hasattr(root, "find") else None
    return walk(body if body is not None else root, 0)


@pattern("page_template", for_=("crawl",))
def _page_template(ctx: PatternContext) -> "Iterable[PatternHint]":
    """The page's template signature (for crawl dedup / clustering: same signature = same
    kind of page -- a listing, a detail page, a login wall)."""
    import hashlib

    root = _pure_tree(ctx)
    if root is None:
        return []
    sig = template_signature(root)
    digest = hashlib.blake2b(sig.encode("utf-8", "replace"), digest_size=8).hexdigest()
    return [PatternHint(name="page_template", subject=digest, count=1, confidence=1.0,
                        evidence=sig[:160], value={"signature": sig})]
