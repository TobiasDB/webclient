"""Generate the Lazy/eager surface from a Core's fields + backing ops.

A core's **data fields** (``model_fields``) and its **backings' typed ops**
(``inspect`` signatures + return hints) are the whole source. Each member's
result type maps into the surface by category (``typeinfo.classify``):

    a Core         -> its surface class      (LazyDocument / Document)
    a scalar        -> Field[T] (lazy) / T (eager)
    an iterable     -> Collection[T] (lazy) / Iterable[T] (eager)

Capabilities give **subtypes**: the base surface = fields + the always-on
backings' ops; each elevated capability (e.g. ``page``) adds a subtype
(``LiveDocument`` = base + the live backing's ops).
"""
from __future__ import annotations

import inspect
import typing
from typing import Any

from . import typeinfo
from .core.document_core import DocumentCore
from .core.reference_core import ReferenceCore
from .core.web_core import WebCore

CORES: tuple[type, ...] = (DocumentCore, ReferenceCore)
#: core -> base surface name (lazy tier prefixes "Lazy")
SURFACE = {DocumentCore: "Document", ReferenceCore: "Reference"}
#: capability -> subtype suffix (base caps produce no subtype)
BASE_CAPS = {"ok", "tree"}
SUBTYPE = {"page": "Live"}


def _type_str(tp: Any) -> str:
    """Render a scalar type to source (best-effort)."""
    tp = typeinfo.unwrap_union(tp)
    if isinstance(tp, type):
        return tp.__name__
    if typing.get_origin(tp) is typing.Literal:
        return "str"                       # a Literal of strings reads as str
    return "Any"


def _render(tp: Any, tier: str) -> str:
    """A member's result type rendered in the given tier's surface vocabulary."""
    cat = typeinfo.classify(tp, CORES)
    if cat == "core":
        base = SURFACE[tp]
        return f"Lazy{base}" if tier == "lazy" else base
    if cat == "iterable":
        el = _render(typeinfo.element_type(tp), tier)
        return f"Collection[{el}]" if tier == "lazy" else f"Iterable[{el}]"
    name = _type_str(tp)
    return f"Field[{name}]" if tier == "lazy" else name


def _params(fn: Any) -> str:
    parts = []
    for p in typeinfo.op_params(fn):
        s = p.name
        if p.kind is p.KEYWORD_ONLY:
            parts = parts + ["*"] if "*" not in parts else parts
        if p.annotation is not p.empty:
            a = p.annotation if isinstance(p.annotation, str) else \
                getattr(p.annotation, "__name__", str(p.annotation))
            s += f": {a}"
        if p.default is not p.empty:
            s += " = ..."
        parts.append(s)
    return "".join(", " + p if p != "*" else ", *" for p in parts)


def _fields(core_cls: type, tier: str) -> list[str]:
    return [f"    {n}: {_render(f.annotation, tier)}"
            for n, f in core_cls.model_fields.items()]


def _ops(backings: list[Any], tier: str) -> list[str]:
    out: list[str] = []
    for backing in backings:
        for op in sorted(backing.provides):
            fn = getattr(backing, op)
            out.append(f"    def {op}(self{_params(fn)}) "
                       f"-> {_render(typeinfo.return_type(fn), tier)}: ...")
    return out


def render_surface(core_cls: type, tier: str) -> str:
    """The base surface class + one subtype per elevated-capability backing."""
    base = SURFACE[core_cls]
    pre = "Lazy" if tier == "lazy" else ""
    by_cap: dict[str, list[Any]] = {}
    for b in core_cls.BACKINGS:
        by_cap.setdefault(b.gate, []).append(b)
    base_backings = [b for g, bs in by_cap.items() if g in BASE_CAPS for b in bs]
    # base = the Core's data fields + the always-on backings' ops
    base_body = _fields(core_cls, tier) + _ops(base_backings, tier)
    blocks = [f"class {pre}{base}:\n" + ("\n".join(base_body) or "    pass")]
    # each elevated capability adds a subtype with only its extra ops (fields
    # inherited from the base)
    for cap, suffix in SUBTYPE.items():
        if cap in by_cap:
            extra = _ops(by_cap[cap], tier)
            blocks.append(f"class {pre}{suffix}{base}({pre}{base}):\n" +
                          ("\n".join(extra) or "    pass"))
    return "\n\n".join(blocks)


def main() -> None:
    for tier in ("lazy", "eager"):
        print(f"# ===== {tier} tier =====")
        print(render_surface(DocumentCore, tier))
        print()


if __name__ == "__main__":
    main()


__all__ = ["render_surface", "CORES", "SURFACE"]
