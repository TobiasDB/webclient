"""Regenerate the static type surfaces that mirror the op registry.

The surface classes (Reference/Document/Collection/Field) carry no op methods
at runtime -- behaviour lives in ``core.ops`` (PLAN §9). Static checkers still
need the op signatures, so they are generated here from the registry and
cannot drift:

1. EAGER tier -- ``if TYPE_CHECKING`` blocks on WebBase / Document / Reference
   giving each op its materialised signature (``select -> Document``,
   ``attr("text") -> Field[str]``). Subclasses inherit WebBase's block.
2. ``Collection[T]`` (base.py) -- the element-op lifting, each op re-typed to
   ``Collection[<ret>]``.
3. LAZY tier (``webclient/stubs.py``) -- every op returns another lazy type;
   ``collect()`` (on ``Lazy[T]``) returns the materialised model.

Every block is rewritten between its ``>>> ... <<<`` markers.

    python scripts/gen_stubs.py          # rewrite
    python scripts/gen_stubs.py --check  # exit 1 if stale (CI / test)
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import webclient.core.base as m  # noqa: E402
import webclient.core.document as d  # noqa: E402
import webclient.core.ops as o  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BASE = Path(m.__file__)           # WebBase eager block + Collection[T] block
DOCS = Path(d.__file__)           # Document + Reference eager blocks
STUBS = ROOT / "webclient" / "stubs.py"

CALL = o.CALL_OPS
PROP = o.PROP_OPS


def _reg(*receivers: str) -> dict[str, object]:
    """Ops registered for these receivers, first receiver wins on a clash."""
    out: dict[str, object] = {}
    for recv in receivers:
        for name, fn in CALL.get(recv, {}).items():
            out.setdefault(name, fn)
    return out


def _raw_ret(fn: object) -> str:
    """The op's declared (string) return annotation."""
    return str(getattr(fn, "__annotations__", {}).get("return", "Any"))


def _params(fn: object, ind: str) -> str:
    """Render a signature's parameters (dropping ``self``) as source."""
    parts = []
    star = False
    for p in inspect.signature(fn).parameters.values():
        if p.name == "self":
            continue
        if p.kind is p.KEYWORD_ONLY and not star:
            parts.append("*")
            star = True
        t = p.name
        if p.kind is p.VAR_POSITIONAL:
            t, star = "*" + t, True
        elif p.kind is p.VAR_KEYWORD:
            t = "**" + t
        if p.annotation is not p.empty:
            ann = p.annotation if isinstance(p.annotation, str) else \
                getattr(p.annotation, "__name__", str(p.annotation))
            t += f": {ann}"
        if p.default is not p.empty:
            t += f" = {p.default!r}"
        parts.append(t)
    return ", ".join(parts)


# ========================================================================= #
# Eager tier -- materialised signatures on WebBase / Document / Reference
# ========================================================================= #

IND8 = "        "


def _eager_ret(fn: object, receiver: str) -> str:
    r = _raw_ret(fn).strip().strip('"\'')
    if r in ("WebBase", "Self"):
        return "Self"
    return r or "Any"


def _eager_attr() -> list[str]:
    return [
        f"{IND8}@overload  # type: ignore[overload-overlap]",
        f"{IND8}def attr(self, name: Literal['href', 'src', 'action'], *, "
        "error: ErrorPolicy | None = None) -> Reference: ...",
        f"{IND8}@overload",
        f"{IND8}def attr(self, name: str, *, error: ErrorPolicy | None = None) "
        "-> Field[str]: ...",
        f"{IND8}def attr(self, name: str, *, error: ErrorPolicy | None = None) "
        "-> Any: ...  # type: ignore[empty-body]",
    ]


def _eager_render() -> list[str]:
    return [
        f"{IND8}@overload",
        f"{IND8}def render(self, format: Literal['markdown', 'text', 'html'], "
        "**options: Any) -> str: ...",
        f"{IND8}@overload",
        f"{IND8}def render(self, format: Literal['elements'], **options: Any) "
        "-> list[Element]: ...",
        f"{IND8}@overload",
        f"{IND8}def render(self, format: Literal['links'], **options: Any) "
        "-> Collection[Reference]: ...",
        f"{IND8}@overload",
        f"{IND8}def render(self, format: str, **options: Any) -> Any: ...",
        f"{IND8}def render(self, format: str, **options: Any) -> Any: "
        "...  # type: ignore[empty-body]",
    ]


def eager_defs(receiver: str) -> list[str]:
    lines: list[str] = []
    for name in sorted(CALL.get(receiver, {})):
        if name == "attr":
            lines += _eager_attr()
            continue
        if name == "render":
            lines += _eager_render()
            continue
        fn = CALL[receiver][name]
        lines.append(
            f"{IND8}def {name}({('self, ' + _params(fn, IND8)).rstrip(', ')}) "
            f"-> {_eager_ret(fn, receiver)}: ...  # type: ignore[empty-body]")
    for name in sorted(PROP.get(receiver, {})):
        fn = PROP[receiver][name]
        lines.append(f"{IND8}@property")
        lines.append(f"{IND8}def {name}(self) -> {_raw_ret(fn)}: "
                     "...  # type: ignore[empty-body]")
    return lines


def eager_block(receiver: str) -> str:
    start = f"{IND8}# >>> eager:{receiver} generated by scripts/gen_stubs.py -- do not edit\n"
    end = f"{IND8}# <<< eager:{receiver}\n"
    return "".join([start, *(ln + "\n" for ln in eager_defs(receiver)), end])


# ========================================================================= #
# Collection[T] element-op overloads (base.py)
# ========================================================================= #

C_START = "        # >>> generated by scripts/gen_stubs.py -- do not edit\n"
C_END = "        # <<< generated\n"


def _lift(ret: str) -> str:
    ret = ret.strip().strip('"\'')
    if ret in ("Self", "T", "WebBase"):
        return "Collection[T]"
    if ret.startswith("Collection["):
        return ret
    return f"Collection[{ret}]"


def _element_ops() -> list[str]:
    seen: dict[str, object] = {}
    for recv in ("Document", "Reference", "WebBase"):
        for name, fn in CALL.get(recv, {}).items():
            if name in m.Collection._WHOLE or name in seen:
                continue
            seen[name] = fn
    lines: list[str] = []
    for name in sorted(seen):
        fn = seen[name]
        if name == "attr":
            lines += [
                f"{IND8}@overload  # type: ignore[overload-overlap]",
                f"{IND8}def attr(self, name: Literal['href', 'src', 'action'], *, "
                "error: ErrorPolicy | None = None) -> Collection[Reference]: ...",
                f"{IND8}@overload",
                f"{IND8}def attr(self, name: str, *, error: ErrorPolicy | None = None) "
                "-> Collection[Field[str]]: ...",
                f"{IND8}def attr(self, name: str, *, error: ErrorPolicy | None = None) "
                "-> Any: ...  # type: ignore[empty-body]",
            ]
            continue
        ret = ("Collection[Reference]" if name == "references"
               else "Collection[Document]" if name == "documents"
               else _lift(_raw_ret(fn)))
        params = ("self, " + _params(fn, IND8)).rstrip(", ")
        lines.append(f"{IND8}def {name}({params}) -> {ret}: "
                     "...  # type: ignore[empty-body]")
    # whole-collection ops (``_WHOLE``): act on the collection itself, not per
    # element -- spelled out here since they carry their own (non-lifted) types.
    lines += [
        f"{IND8}def extract(self, *, error: ErrorPolicy | None = None, "
        "**named_expr: Any) -> Collection[T]: ...  # type: ignore[empty-body]",
        f"{IND8}def filter(self, *expr: Any, error: ErrorPolicy | None = None, "
        "**named_expr: Any) -> Collection[T]: ...  # type: ignore[empty-body]",
        f"{IND8}def project[M](self, model: type[M] | None = None, *, "
        "error: ErrorPolicy | None = None) -> list[M | dict[str, Any]]: "
        "...  # type: ignore[empty-body]",
        f"{IND8}def is_ok(self, *, error: ErrorPolicy | None = None) "
        "-> Field[bool]: ...  # type: ignore[empty-body]",
        f"{IND8}def is_empty(self, *, error: ErrorPolicy | None = None) "
        "-> Field[bool]: ...  # type: ignore[empty-body]",
    ]
    return lines


def collection_block() -> str:
    return "".join([C_START, *(ln + "\n" for ln in _element_ops()), C_END])


# ========================================================================= #
# Lazy Protocol tiers (stubs.py)
# ========================================================================= #

L_START = "# >>> generated by scripts/gen_stubs.py -- do not edit\n"
L_END = "# <<< generated\n"

_SAFE = {"str", "int", "bool", "float", "Any", "None",
         "str | None", "int | None", "bool | None", "float | None",
         "Sequence[str]"}


def _lazy_ret(ret: str, selfty: str) -> str:
    r = ret.strip().strip('"\'')
    if r in ("Self", "WebBase"):
        return selfty
    if r.startswith("Collection"):
        return '"LazyCollection"'
    if r == "Document":
        return '"LazyDocument"'
    if r == "Reference":
        return '"LazyReference"'
    if r.startswith("Field"):
        inner = r[len("Field"):].strip()
        inner = inner[1:-1] if inner.startswith("[") else ""
        return f'"LazyField[{inner or "Any"}]"'
    return "Any"


def _pann(p: inspect.Parameter) -> str:
    if p.name == "error":
        return "_E"
    if p.annotation is p.empty:
        return "Any"
    ann = p.annotation if isinstance(p.annotation, str) else \
        getattr(p.annotation, "__name__", str(p.annotation))
    return ann if ann in _SAFE else "Any"


def _lazy_params(fn: object) -> str:
    parts: list[str] = []
    star = False
    for p in inspect.signature(fn).parameters.values():
        if p.name == "self":
            continue
        if p.kind is p.VAR_POSITIONAL:
            parts.append(f"*{p.name}: {_pann(p)}")
            star = True
            continue
        if p.kind is p.VAR_KEYWORD:
            parts.append(f"**{p.name}: {_pann(p)}")
            continue
        if p.kind is p.KEYWORD_ONLY and not star:
            parts.append("*")
            star = True
        t = f"{p.name}: {_pann(p)}"
        if p.default is not p.empty:
            t += " = ..."
        parts.append(t)
    return ", ".join(parts)


def _method(name: str, fn: object, selfty: str, forced: str | None = None) -> list[str]:
    ret = forced or _lazy_ret(_raw_ret(fn), selfty)
    params = _lazy_params(fn)
    sig = "self" + (f", {params}" if params else "")
    return [f"    def {name}({sig}) -> {ret}: ..."]


def _attr_overloads(link: str, field: str) -> list[str]:
    return [
        '    @overload',
        f'    def attr(self, name: _LINK, *, error: _E = ...) -> {link}: ...  # type: ignore[overload-overlap]',
        '    @overload',
        f'    def attr(self, name: str, *, error: _E = ...) -> {field}: ...',
    ]


def _lazy_field() -> list[str]:
    ops = ["eq", "ne", "lt", "le", "gt", "ge", "and", "or"]
    dunder = {"eq": "__eq__", "ne": "__ne__", "lt": "__lt__", "le": "__le__",
              "gt": "__gt__", "ge": "__ge__", "and": "__and__", "or": "__or__"}
    lines = [
        'class LazyField[T](Lazy["Field[T]"], Protocol):',
        '    """A lazy scalar; ``collect()`` -> ``Field[T]`` (then ``.get()`` -> T)."""',
        '    def get(self) -> T: ...',
        '    def is_ok(self, *, error: _E = ...) -> "LazyField[bool]": ...',
        '    def is_empty(self, *, error: _E = ...) -> "LazyField[bool]": ...',
    ]
    for op in ops:
        ignore = "  # type: ignore[override]" if op in ("eq", "ne") else ""
        lines.append(f'    def {dunder[op]}(self, o: Any) -> "LazyField[bool]": ...{ignore}')
    lines.append('    def __invert__(self) -> "LazyField[bool]": ...')
    return lines


def _lazy_reference() -> list[str]:
    lines = ['class LazyReference(Lazy["Reference"], Protocol):']
    for name, fn in sorted(_reg("Reference").items()):
        lines += _method(name, fn, '"LazyReference"')
    return lines


def _lazy_document() -> list[str]:
    ops = _reg("Document", "WebBase")
    lines = ['class LazyDocument(Lazy["Document"], Protocol):']
    for name in sorted(ops):
        if name == "attr":
            lines += _attr_overloads('"LazyReference"', '"LazyField[str]"')
            continue
        lines += _method(name, ops[name], '"LazyDocument"')
    return lines


def _lazy_collection() -> list[str]:
    lines = ['class LazyCollection(Lazy["Collection[Any]"], Protocol):']
    spec = [("select", CALL["Document"]["select"]),
            ("select_all", CALL["Document"]["select_all"]),
            ("extract", CALL["Collection"]["extract"]),
            ("filter", CALL["Collection"]["filter"]),
            ("references", CALL["WebBase"]["references"]),
            ("documents", CALL["WebBase"]["documents"])]
    for name, fn in spec:
        lines += _method(name, fn, '"LazyCollection"', forced='"LazyCollection"')
    lines.append('    def attr(self, name: str, *, error: _E = ...) -> "LazyCollection": ...')
    for name in ("is_ok", "is_empty"):
        lines += _method(name, CALL["WebBase"][name], '', forced='"LazyField[bool]"')
    lines.append('    def project(self, model: Any = ..., *, error: _E = ...) -> Any: ...')
    return lines


def lazy_block() -> str:
    body: list[str] = []
    for chunk in (_lazy_field(), _lazy_reference(), _lazy_document(),
                  _lazy_collection()):
        body += chunk
        body.append("")
    return "".join([L_START, *(ln + "\n" for ln in body), L_END])


# ========================================================================= #
# Driver
# ========================================================================= #

def _regions() -> list[tuple[Path, str, str, str]]:
    regions = [
        (BASE, C_START, C_END, collection_block()),
        (STUBS, L_START, L_END, lazy_block()),
    ]
    for path, recv in ((BASE, "WebBase"), (DOCS, "Document"), (DOCS, "Reference")):
        start = f"{IND8}# >>> eager:{recv} generated by scripts/gen_stubs.py -- do not edit\n"
        end = f"{IND8}# <<< eager:{recv}\n"
        if start in path.read_text():            # only if the marker is present
            regions.append((path, start, end, eager_block(recv)))
    return regions


def main(check: bool) -> int:
    rc = 0
    for path, start, end, block in _regions():
        text = path.read_text()
        i, j = text.index(start), text.index(end) + len(end)
        new = text[:i] + block + text[j:]
        if new == text:
            continue
        if check:
            print(f"{path.name} generated block is stale: run scripts/gen_stubs.py")
            rc = 1
            continue
        path.write_text(new)
        print(f"{path.name} generated block regenerated")
    return rc


if __name__ == "__main__":
    sys.exit(main("--check" in sys.argv))
