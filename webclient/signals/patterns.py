"""Pattern detectors: recurring-STRUCTURE signals -- the repeating record list(s) to extract, the
repeated controls to act on per item, and the page-template signature for crawl clustering. Expressed
through the ONE Signals/Flags registry (``@detector`` -> a flag whose value is a list of
:class:`PatternHint`), NOT a parallel registry. DOM recon on the pure toolkit (:mod:`webclient.dom`);
visual + behaviour recon are later kinds.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from ..core.document.models import PatternHint
from .context import Context
from .registry import Hit, detector, flag

if TYPE_CHECKING:
    from ..core.document.models import Signal


def _pure_tree(ctx: Context) -> Any:
    """The context's lxml root, but only when it is a real element tree (not a treeless context)."""
    return ctx.tree if ctx.tree is not None and isinstance(getattr(ctx.tree, "tag", None), str) else None


def template_signature(root: Any, *, budget: int = 3) -> str:
    """A page-TEMPLATE signature: the structural signature of the body's top levels (tags, ids,
    semantic classes -- no text), so pages built from the same template share it and a crawl can
    treat them as one kind of page. Bounded depth keeps it stable across data."""
    from ..dom.skeleton import kept_children, selector_sig

    def walk(el: Any, depth: int) -> str:
        kids = kept_children(el)
        inner = "" if depth >= budget else ",".join(walk(k, depth + 1) for k in kids[:12])
        return f"{selector_sig(el)}({inner})"

    body = root.find("body") if hasattr(root, "find") else None
    return walk(body if body is not None else root, 0)


@detector(flag="record_regions", name="record_list", stage="static")
def _record_list(ctx: Context) -> "Hit | None":
    """The repeating dataset regions (MDR-style dominant sibling groups): each is a ``select_all``
    target for extraction. Confidence grows with group size + content richness vs the best region.
    The flag value is the list of :class:`PatternHint` (most confident first)."""
    from ..dom.records import find_record_regions

    root = _pure_tree(ctx)
    if root is None:
        return None
    regions = find_record_regions(root, top_k=3)
    if not regions:
        return None
    best = regions[0].score or 1.0
    hints = [
        PatternHint(
            name="record_list", subject=r.item_selector, count=r.count, for_=("extract",),
            confidence=round(min(1.0, 0.5 + 0.5 * (r.score / best)), 3),
            evidence=f"{r.count} structurally identical siblings under <{r.container_tag}"
                     + (f"#{r.container_id}" if r.container_id else "") + ">",
        )
        for r in regions
    ]
    return Hit(hints[0].confidence, f"{len(hints)} record region(s)", hints)


@detector(flag="repeated_controls", name="repeated_control", stage="static")
def _repeated_control(ctx: Context) -> "Hit | None":
    """Interactive controls that repeat with the same structure + label shape (an "add to cart" per
    card, a "load more" per section): one action per item. Each hint's ``subject`` is a durable
    selector for the first instance; ``count`` how many share it."""
    from ..dom.index import durable_selector
    from ..dom.interactivity import interactive
    from ..dom.naming import name as _name
    from ..dom.skeleton import selector_sig

    root = _pure_tree(ctx)
    if root is None:
        return None
    groups: dict[tuple[str, str], list[Any]] = {}
    for el in root.iter():
        if not isinstance(getattr(el, "tag", None), str) or interactive(el) is None:
            continue
        named = _name(el)
        key = (selector_sig(el), (named.label if named else "").lower())
        groups.setdefault(key, []).append(el)
    hints: list[PatternHint] = []
    for (sig, label), els in groups.items():
        if len(els) < 3 or not label:
            continue
        hints.append(PatternHint(
            name="repeated_control", subject=durable_selector(els[0]), count=len(els),
            for_=("interact",), confidence=round(min(1.0, 0.4 + 0.1 * len(els)), 3),
            evidence=f'{len(els)} × {sig} labelled "{label}"', value={"label": label},
        ))
    if not hints:
        return None
    hints.sort(key=lambda h: h.confidence, reverse=True)
    return Hit(hints[0].confidence, f"{len(hints)} repeated control group(s)", hints)


@detector(flag="page_template", name="page_template", stage="static")
def _page_template(ctx: Context) -> "Hit | None":
    """The page's template signature (for crawl dedup / clustering: same signature = same kind of
    page -- a listing, a detail page, a login wall). One hint, ``subject`` the signature digest."""
    root = _pure_tree(ctx)
    if root is None:
        return None
    sig = template_signature(root)
    digest = hashlib.blake2b(sig.encode("utf-8", "replace"), digest_size=8).hexdigest()
    hint = PatternHint(name="page_template", subject=digest, count=1, for_=("crawl",),
                       confidence=1.0, evidence=sig[:160], value={"signature": sig})
    return Hit(1.0, sig[:160], [hint])


def _hints_value(signals: "list[Signal]", ctx: Context) -> Any:
    """A pattern flag's value: the :class:`PatternHint` list carried by the detector that fired
    (each pattern flag has one detector), else ``None``."""
    for s in signals:
        if isinstance(s.value, list):
            return s.value
    return None


flag("record_regions", value=_hints_value, group="pattern")
flag("repeated_controls", value=_hints_value, group="pattern")
flag("page_template", value=_hints_value, group="pattern")


__all__ = ["template_signature"]
