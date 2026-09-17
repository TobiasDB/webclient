"""Regenerate the typed surface stubs from the cores + their backings.

One function -- ``members`` -- reads a Core's data fields, its class properties
and its backings' typed ops and renders each into a tier's vocabulary:

    Core subtype   ->  Document / Reference   (eager) | LazyDocument / ...  (lazy)
    scalar T       ->  T  or  Field[T]        (eager) | LazyField[T]        (lazy)
    Iterable[Core] ->  Collection[Surface]    (eager) | LazyCollection[...] (lazy)

The op signatures live on the backings -- the single source of truth -- so the
same ``members`` walk emits every surface (Reference / Document, eager + lazy)
plus the Collection element-op lift. Nothing is duplicated in a table here.

    python scripts/gen_stubs.py          # rewrite the blocks
    python scripts/gen_stubs.py --check  # exit 1 if any block is stale (CI)
"""

from __future__ import annotations

import inspect
import logging
import sys
import types as _types
import typing
from collections.abc import Iterable as _Iterable
from collections.abc import Mapping as _Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webclient.clients import WaitConfig  # noqa: E402
from webclient.collection import Field  # noqa: E402
from webclient.core.client import WebClient  # noqa: E402
from webclient.core.crawl import Crawl  # noqa: E402
from webclient.core.document import Document, Element  # noqa: E402
from webclient.core.reference import Reference  # noqa: E402
from webclient.core.session import Session  # noqa: E402
from webclient.core.document.models import (  # noqa: E402
    Flag,
    Metadata,
    Signal,
    Structure,
    Transport,
)
from webclient.models import (  # noqa: E402
    ActionEvent,
    ConsoleEvent,
    DOMUpdateEvent,
    Event,
)
from webclient.models import E as _EventTypeVar  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SURFACES = ROOT / "webclient" / "surfaces" / "eager.py"
COLLECTION = ROOT / "webclient" / "collection.py"
MODELS = ROOT / "webclient" / "surfaces" / "lazy.py"
#: cores that implement their own eager ops -- the generated ``I<Core>`` interface
#: is emitted into the core's own module (which the core inherits).
DOCINIT = ROOT / "webclient" / "core" / "document" / "models.py"
REFINIT = ROOT / "webclient" / "core" / "reference" / "models.py"
CLIENTMODELS = ROOT / "webclient" / "core" / "client" / "models.py"
CRAWLMODELS = ROOT / "webclient" / "core" / "crawl" / "models.py"

#: the cores that map to a surface class (a Core-typed result -> its surface).
CORES: tuple[type, ...] = (Reference, Document, Crawl)
SURFACE = {Reference: "Reference", Document: "Document", Crawl: "Crawl"}
LAZY = {Reference: "LazyReference", Document: "LazyDocument"}
#: the async eager tier: a Core maps to its Async surface, and its IO ops are
#: ``async def`` (see ``members``), so ``await ac.ref(url).resolve()`` types.
SURFACE_ASYNC = {Reference: "AsyncReference", Document: "AsyncDocument"}
#: the cores whose ops the core itself implements via a generated interface (so the
#: async surface's IO ops override the inherited eager ones -> need an ``override``
#: ignore). Grows as each core gets its interface.
HAS_INTERFACE: set[type] = {Document, Reference, WebClient}
#: bare core-surface names -- an overload returning one overlaps a later ``str``
#: overload and needs the ``overload-overlap`` ignore.
_CORE_SURFACES = (
    set(SURFACE.values()) | set(LAZY.values()) | set(SURFACE_ASYNC.values())
)

#: names the resolved annotations may reference (TYPE_CHECKING-only in their own
#: modules), merged into each fn's globals for ``get_type_hints``.
_NS = {
    "Document": Document,
    "Crawl": Crawl,
    "Reference": Reference,
    "WebClient": WebClient,
    "Field": Field,
    "Element": Element,
    "Transport": Transport,
    "Metadata": Metadata,
    "Signal": Signal,
    "Flag": Flag,
    "Structure": Structure,
    "Event": Event,
    "ActionEvent": ActionEvent,
    "ConsoleEvent": ConsoleEvent,
    "DOMUpdateEvent": DOMUpdateEvent,
    "E": _EventTypeVar,
    "Any": Any,
    "WaitConfig": WaitConfig,
}
_SCALAR = {str: "str", int: "int", float: "float", bytes: "bytes", bool: "bool"}
_UNION = (typing.Union, getattr(_types, "UnionType", None))
_SKIP_FIELDS = {Reference: set[str](), Document: set[str]()}


# -- type classification (was webclient/typeinfo.py; only the generator uses it)


log = logging.getLogger("gen_stubs")


def _is_model(tp: Any) -> bool:
    """A pydantic data model (e.g. ``Summary``) -- NOT a Core (those map to their
    surface). A model return is a terminal value collected as a ``Lazy[T]``."""
    from pydantic import BaseModel

    return isinstance(tp, type) and issubclass(tp, BaseModel) and tp not in CORES


def _classify(tp: Any) -> str:
    """-> 'core' (a mapped Core -> its surface), 'iterable' (list/Sequence ->
    Collection[T]) or 'scalar' (dict/Mapping/scalars stay data)."""
    if isinstance(tp, type) and issubclass(tp, CORES):
        return "core"
    origin = typing.get_origin(tp)
    if (
        origin is not None
        and isinstance(origin, type)
        and issubclass(origin, _Iterable)
        and not issubclass(origin, _Mapping)
        and origin not in (str, bytes)
    ):
        return "iterable"
    return "scalar"


def _element_type(tp: Any) -> Any:
    """The element type of a list/Sequence annotation (``Any`` if unparameterised)."""
    args = typing.get_args(tp)
    return args[0] if args else Any


def _is_optional(tp: Any) -> bool:
    """Whether ``tp`` is a union that includes ``None`` (an ``X | None``)."""
    return typing.get_origin(tp) in _UNION and type(None) in typing.get_args(tp)


def _unwrap_union(tp: Any) -> Any:
    """A union -> its single non-None member (else ``Any``); a plain type passes."""
    if typing.get_origin(tp) in _UNION:
        parts = [a for a in typing.get_args(tp) if a is not type(None)]
        return parts[0] if len(parts) == 1 else Any
    return tp


def _field_type(core_cls: type, name: str) -> Any:
    """A Core data field's type (pydantic ``model_fields``)."""
    field = getattr(core_cls, "model_fields", {}).get(name)
    return field.annotation if field is not None else Any


# -- type rendering -----------------------------------------------------------


class _Unresolved(Exception):
    """A backing annotation the generator could not resolve to a real type --
    a bug to fix loudly, never a silent ``Any`` in the emitted stub."""


def _return(fn: Any) -> Any:
    """The resolved return annotation of ``fn`` (``Any`` if unannotated). An
    annotation that is present but unresolvable is a hard error -- the generator
    must never silently degrade a member to ``Any``."""
    raw = getattr(fn, "__annotations__", {}).get("return")
    if raw is None:
        return Any
    try:
        ns = {**getattr(fn, "__globals__", {}), **_NS}
        return typing.get_type_hints(fn, globalns=ns).get("return", Any)
    except Exception as exc:
        where = f"{getattr(fn, '__qualname__', fn)}"
        raise _Unresolved(
            f"cannot resolve return annotation {raw!r} of {where}: {exc}. "
            "Add the name to gen_stubs._NS or import it in the backing's module."
        ) from exc


def _name(tp: Any) -> str:
    """A plain type name for annotations that are not surface-mapped."""
    if tp is Any or tp is inspect.Parameter.empty:
        return "Any"
    if tp is Element:
        return "Element"
    if tp in _SCALAR:
        return _SCALAR[tp]
    if isinstance(tp, str):
        return tp
    origin = typing.get_origin(tp)
    if origin in _UNION:
        return " | ".join(_name(a) for a in typing.get_args(tp))
    if origin is not None:
        base = {list: "list", dict: "dict", tuple: "tuple", set: "set"}.get(
            origin, getattr(origin, "__name__", "Any")
        )
        args = ", ".join(_name(a) for a in typing.get_args(tp))
        return f"{base}[{args}]" if args else base
    return getattr(tp, "__name__", None) or "Any"


def _render(tp: Any, tier: str) -> str:
    """Map an op's return type into the tier's vocabulary (see module docstring).
    ``client`` is like ``lazy`` (a Core maps to its lazy surface) but a value
    return is a ``Lazy[T]`` handle -- a client verb records a plan you collect,
    not a chainable field/list."""
    inner = _unwrap_union(tp)
    cat = _classify(inner)
    smap = {"eager": SURFACE, "async": SURFACE_ASYNC}.get(tier, LAZY)
    sync = tier in ("eager", "async")  # a materialised value tier
    if cat == "core":
        return smap[inner]
    if cat == "iterable":
        el = _unwrap_union(_element_type(inner))
        if _classify(el) == "core":
            sub = smap[el]
            box = "Collection" if sync else "LazyCollection"
            return f"{box}[{sub}]"
        listed = f"list[{_name(el)}]"  # an iterable of non-cores stays a list
        return f"Lazy[{listed}]" if tier == "client" else listed
    if typing.get_origin(inner) is Field:  # a value leaf
        base = _name(_element_type(inner))
        return f"Field[{base}]" if sync else f"LazyField[{base}]"
    base = _name(inner)  # a plain scalar or a pydantic data model
    if sync:
        # preserve optionality for a plain scalar value (e.g. text_content/title ->
        # str | None), so the type doesn't lie on the lenient chain; a core return
        # (Reference | None) is handled above and keeps its surface name.
        if _is_optional(tp) and not _is_model(inner):
            return f"{base} | None"
        return base
    if tier == "client":
        return f"Lazy[{base}]"
    # lazy tier: a scalar leaf is Field-wrapped by ``_materialize`` (chainable); a
    # data model is a terminal value you collect -- a ``Lazy[T]`` handle, like
    # ``project()`` -- since ``_materialize`` leaves a model unwrapped.
    if _is_model(inner):
        return f"Lazy[{base}]"
    return f"LazyField[{base}]"


def _field(tp: Any, tier: str) -> str:
    """A Core data field: eager keeps the (scalar) python type, lazy wraps it."""
    origin = typing.get_origin(tp)
    if tp in _SCALAR:
        name = _SCALAR[tp]
    elif origin is typing.Literal:
        name = "str"
    elif origin in _UNION:
        rest = [a for a in typing.get_args(tp) if a is not type(None)]
        name = (
            f"{_SCALAR[rest[0]]} | None"
            if len(rest) == 1 and rest[0] in _SCALAR
            else "Any"
        )
    else:
        name = "Any"
    return name if tier == "eager" else f"LazyField[{name}]"


# -- signature reading --------------------------------------------------------


def _params(fn: Any) -> str:
    """The op's parameter list (dropping ``self``/``core``), defaults as ``...``.
    Annotations are the source strings (PEP 563), used verbatim."""
    parts: list[str] = []
    star = False
    for name, p in inspect.signature(fn).parameters.items():
        if name in ("self", "core"):
            continue
        ann = "" if p.annotation is inspect.Parameter.empty else f": {p.annotation}"
        if p.kind is p.VAR_POSITIONAL:
            parts.append(f"*{name}{ann}")
            star = True
        elif p.kind is p.VAR_KEYWORD:
            parts.append(f"**{name}{ann}")
        else:
            if p.kind is p.KEYWORD_ONLY and not star:
                parts.append("*")
                star = True
            default = " = ..." if p.default is not inspect.Parameter.empty else ""
            parts.append(f"{name}{ann}{default}")
    return ", ".join(parts)


def _fn(backing: Any, op: str) -> Any:
    return getattr(type(backing), op)


def _provider(core: type, op: str, kind: str) -> Any:
    """The backing that provides ``op`` with the richest signature (so a shared
    op like ``select_all`` shows its full form, not a narrower live variant).
    ``kind`` is ``'provides'`` (call ops) or ``'props'`` (property ops)."""
    cands = [b for b in core.BACKINGS if op in getattr(b, kind)]
    return max(cands, key=lambda b: len(inspect.signature(_fn(b, op)).parameters))


def _method(op: str, fn: Any, tier: str) -> list[str]:
    """One call op -> its def line(s), expanding ``@overload`` sets."""
    overloads = typing.get_overloads(fn)
    if len(overloads) <= 1:
        ret = _render(_return(fn), tier)
        return [
            f'def {op}(self, {_params(fn)}) -> "{ret}": ...'.replace(
                "(self, )", "(self)"
            )
        ]
    lines: list[str] = []
    for ov in overloads:
        ret = _render(_return(ov), tier)
        ignore = "  # type: ignore[overload-overlap]" if ret in _CORE_SURFACES else ""
        line = f'def {op}(self, {_params(ov)}) -> "{ret}": ...{ignore}'.replace(
            "(self, )", "(self)"
        )
        lines += ["@overload", line]
    return lines


# -- the one surface walk -----------------------------------------------------


def _class_props(core: type) -> dict[str, Any]:
    """Plain ``@property`` members on the core class (e.g. ``ok``)."""
    return {
        name: val.fget
        for name, val in vars(core).items()
        if isinstance(val, property) and not name.startswith("model_")
    }


def members(
    core: type, tier: str, *, fields: bool = True, class_props: bool = True
) -> list[str]:
    """Every surface member for ``core`` in ``tier`` -- data fields, class
    properties, then the backings' property ops and call ops. This is the whole
    generator: every surface (eager, lazy, the lift, the client) comes from it.
    ``fields``/``class_props`` drop the data model for an authoring surface (the
    client), whose only members are its verbs."""
    lines: list[str] = []
    if fields:
        for name in core.model_fields:
            if name in _SKIP_FIELDS.get(core, set()):
                continue
            lines.append(f"{name}: {_field(_field_type(core, name), tier)}")
    # property ops (class @property + backing props) -- an attribute when lazy,
    # a @property when eager.
    seed = _class_props(core) if class_props else {}
    props = {**seed, **{op: None for op in core.prop_ops()}}
    for op in sorted(props):
        fn = props[op] or _fn(_provider(core, op, "props"), op)
        ret = _render(_return(fn), tier)
        if tier != "lazy":  # a real @property on the eager/surface/async view
            lines += ["@property", f"def {op}(self) -> {ret}: ..."]
        else:  # the lazy tier records attribute access, so a prop is an attribute
            lines.append(f'{op}: "{ret}"')
    # call ops -- in the async tier the IO ops (resolve/fetch/summary) are
    # ``async def`` (awaited -> the async surface); the rest stay synchronous.
    io = core.io_ops() if tier == "async" else frozenset()
    # when the core implements its ops (via its interface), the async surface's IO
    # ops override the inherited eager ones with an ``async def`` -> silence override.
    override = "  # type: ignore[override]" if core in HAS_INTERFACE else ""
    for op in sorted(core.ops()):
        method = _method(op, _fn(_provider(core, op, "provides"), op), tier)
        if op in io:
            method = [
                ln.replace(f"def {op}(", f"async def {op}(") + override
                for ln in method
            ]
        lines += method
    return lines


def _lift(
    op: str, fn: Any, tier: str = "eager", *, is_prop: bool = False
) -> str | None:
    """The Collection-lifted form of a Document op, in ``tier`` vocabulary:
    ``T -> Collection[T]`` / ``scalar -> Collection[Field[scalar]]`` (eager), or
    the ``Lazy*`` mirror (lazy); ``None`` to skip (non-liftable). An overloaded op
    lifts by its broadest (last) overload. A lazy prop lift is a recorded
    attribute (like ``LazyDocument.text_content``), not a method."""
    box = "Collection" if tier == "eager" else "LazyCollection"
    surfaces = set(SURFACE.values()) if tier == "eager" else set(LAZY.values())
    field = "Field" if tier == "eager" else "LazyField"
    overloads = typing.get_overloads(fn)
    ret = _render(_return(overloads[-1] if overloads else fn), tier)
    if ret in surfaces or ret.startswith(f"{box}["):
        inner = ret[len(f"{box}[") : -1] if ret.startswith(f"{box}[") else ret
        lifted = f"{box}[{inner}]"
    elif ret.startswith(f"{field}["):
        lifted = f"{box}[{ret}]"
    elif ret in _SCALAR.values():  # an eager scalar stays raw -> wrap on lift
        lifted = f"{box}[{field}[{ret}]]"
    else:
        return None  # Any / list / dict -- nothing sensible to lift
    if tier != "eager" and is_prop:  # recorded as a bare attribute access
        return f'{op}: "{lifted}"'
    params = _params(overloads[-1] if overloads else fn)
    sig = f"self, {params}" if params else "self"
    return f'def {op}({sig}) -> "{lifted}": ...'


def lift_members(tier: str = "eager") -> list[str]:
    """Element ops lifted onto a Collection (fan-out keeps the element type),
    in ``tier`` vocabulary (eager Collection, or lazy LazyCollection)."""
    lines: list[str] = []
    for op in sorted(set(Document.ops()) | set(Document.prop_ops())):
        is_prop = op not in Document.ops()
        kind = "provides" if op in Document.ops() else "props"
        row = _lift(
            op, _fn(_provider(Document, op, kind), op), tier, is_prop=is_prop
        )
        if row is not None:
            lines.append(row)
    return lines


# -- lazy leaf helpers (value/iterable leaves; hand-written like Field/Collection)


_LAZY_FIELD = """class LazyField(Lazy["Field[S]"], Generic[S]):
    def get(self, default: Any = ...) -> S: ...
    def is_ok(self) -> "LazyField[bool]": ...
    def is_empty(self) -> "LazyField[bool]": ...
    def __eq__(self, o: Any) -> "LazyField[bool]": ...  # type: ignore[override]
    def __ne__(self, o: Any) -> "LazyField[bool]": ...  # type: ignore[override]
    def __and__(self, o: Any) -> "LazyField[bool]": ...
    def __or__(self, o: Any) -> "LazyField[bool]": ...
    def __invert__(self) -> "LazyField[bool]": ...
    def collect(self, context: Any = ...) -> "Field[S]": ..."""

#: LazyCollection's row-shaping ops (the lazy mirror of Collection's own methods);
#: the element-op lift (select/attr/text_content/...) is generated alongside.
_LAZY_COLLECTION_SHAPING = [
    'def extract(self, **exprs: Any) -> "LazyCollection[T]": ...',
    'def filter(self, *predicates: Any) -> "LazyCollection[T]": ...',
    'def limit(self, n: int) -> "LazyCollection[T]": ...',
    'def documents(self, column: str) -> "LazyCollection[LazyDocument]": ...',
    'def project(self) -> "Lazy[list[dict[str, Any]]]": ...',
    'def collect(self, context: Any = ...) -> "Collection[T]": ...',
]


def _lazy_collection() -> str:
    """LazyCollection: the lazy element-op lift (fan-out keeps the element type)
    plus the row-shaping ops -- the lazy mirror of the eager Collection."""
    body = lift_members("lazy") + _LAZY_COLLECTION_SHAPING
    head = 'class LazyCollection(Lazy["Collection[T]"], Generic[T]):'
    return head + "\n" + "\n".join("    " + line for line in body)


def _lazy_class(core: type) -> str:
    """A derived lazy surface: the same members as eager, in lazy vocabulary,
    plus the recorder-only helpers (``field``/``reference``) and ``collect``."""
    extras: list[str] = []
    if core is Document:
        extras += [
            # row-shaping on a lone document (the single-element form of the
            # Collection ops); hand-written like the eager Document.extract/project.
            'def extract(self, **exprs: Any) -> "LazyDocument": ...',
            'def project(self) -> "Lazy[dict[str, Any]]": ...',
            'def field(self, name: str) -> "LazyField[Any]": ...',
            'def reference(self, name: str) -> "LazyReference": ...',
        ]
    extras.append(f'def collect(self, context: Any = ...) -> "{SURFACE[core]}": ...')
    body = members(core, "lazy") + extras
    head = f'class {LAZY[core]}(Lazy["{SURFACE[core]}"]):'
    return head + "\n" + "\n".join("    " + line for line in body)


def _lazy_client_class() -> str:
    """LazyWebClient: the lazy recorder rooted at a client/session (``wc.lazy``).
    An authoring root, not a collectable ``Lazy[Surface]`` -- so its verbs use the
    ``client`` tier (a value return is a ``Lazy[T]`` handle you ``.collect()``).
    Generated from ``WebClient``'s verbs, like every other tier."""
    body = members(WebClient, "client", fields=False, class_props=False)
    lines = [
        "class LazyWebClient:",
        '    """The lazy recorder rooted at a client/session (``wc.lazy``): its verbs',
        "    record a ``WebClient``-rooted plan to batch/defer -- ``wc.lazy.fetch(url)",
        "    .collect()`` records then runs on the client's engine.\"\"\"",
        *("    " + line for line in body),
    ]
    return "\n".join(lines)


def _lazy_tier() -> str:
    blocks = [
        _LAZY_FIELD,
        _lazy_class(Reference),
        _lazy_class(Document),
        _lazy_collection(),
        _lazy_client_class(),
    ]
    return "# fmt: off\n" + "\n\n\n".join(blocks) + "\n# fmt: on"


# -- block assembly / rewrite -------------------------------------------------


def _indented(lines: list[str], indent: int) -> str:
    pad = " " * indent
    rows = [pad + "# fmt: off", *(pad + line for line in lines), pad + "# fmt: on"]
    return "\n".join(rows)


def _body(region: str) -> str:
    if region == "lazy-tier":
        return _lazy_tier()
    if region == "Reference interface":
        # the eager ops Reference implements (its ``IReference`` interface).
        return _indented(
            members(Reference, "eager", fields=False, class_props=False), 8
        )
    if region == "Document interface":
        # the eager ops Document implements (its ``IDocument`` interface, which
        # the core inherits) -- so a backing/the core reaches them statically.
        return _indented(
            members(Document, "eager", fields=False, class_props=False), 8
        )
    if region == "collection element-op lifting":
        return _indented(lift_members(), 8)
    if region == "WebClient interface":
        # the eager verbs WebClient implements (its ``IWebClient`` interface,
        # which the core -- and so Session -- inherits).
        return _indented(
            members(WebClient, "eager", fields=False, class_props=False), 8
        )
    if region == "Crawl interface":
        return _indented(
            members(Crawl, "eager", fields=False, class_props=False), 8
        )
    if region == "AsyncReference surface":
        # the async view: IO ops (resolve) are ``async def``, Core returns map to
        # the Async surfaces so ``await ac.ref(url).resolve()`` chains async.
        return _indented(
            members(Reference, "async", fields=False, class_props=False), 8
        )
    if region == "AsyncDocument surface":
        return _indented(
            members(Document, "async", fields=False, class_props=False), 8
        )
    if region == "AsyncWebClient surface":
        # the async client's verbs: ``async def fetch/summary`` -> the async
        # surface, sync ``ref`` -> AsyncReference.
        return _indented(
            members(WebClient, "async", fields=False, class_props=False), 8
        )
    raise KeyError(region)


REGIONS = [
    (REFINIT, "Reference interface"),
    (DOCINIT, "Document interface"),
    (CLIENTMODELS, "WebClient interface"),
    (CRAWLMODELS, "Crawl interface"),
    (SURFACES, "AsyncReference surface"),
    (SURFACES, "AsyncDocument surface"),
    (SURFACES, "AsyncWebClient surface"),
    (COLLECTION, "collection element-op lifting"),
    (MODELS, "lazy-tier"),
]


def main(check: bool) -> int:
    stale: list[str] = []
    for path, region in REGIONS:
        text = path.read_text()
        start = f"# >>> generated: {region} <<<\n"
        lo = text.index(start) + len(start)
        e = text.index(">>> end generated <<<", lo)
        hi = text.rindex("\n", lo, e) + 1  # start of the end-marker line
        body = _body(region) + "\n"
        if text[lo:hi] != body:
            if check:
                stale.append(f"{path.name}: {region}")
            else:
                path.write_text(text[:lo] + body + text[hi:])
    if check and stale:
        log.error("stale generated stubs:\n  %s", "\n  ".join(stale))
        return 1
    log.info("stubs up to date" if check else "stubs regenerated")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    sys.exit(main(check="--check" in sys.argv))
