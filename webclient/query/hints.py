"""Runtime return-type resolution from a backing op's own overloads.

The typed overloads on the backings are the single source of truth: the static
stubs are generated from them (``scripts.gen_stubs``) AND the recorder reads them
at run time to know the return surface of each recorded op. So a value can be the
*matching* real class -- ``attr("href") -> Reference``, ``attr("x") -> Field`` --
with no separate table: ``resolve_hints`` picks the first overload whose params
bind and type-check against the actual call args (exactly what a static checker
does), and ``return_type`` reads its ``return`` annotation.

The result is a raw annotation (a ``Core`` type, ``Field[str]``, ``list[Core]``,
a scalar, ``Self`` ...); mapping it to a tier's surface class is the recorder's
job. ``return_type`` also covers a core's pydantic data fields (``model_fields``)
and property ops, not just call ops.
"""

from __future__ import annotations

import inspect
import typing
from typing import Any

from typeguard import TypeCheckError, check_type

_MISSING: Any = object()
_NS: dict[str, Any] | None = None


def _ns() -> dict[str, Any]:
    """The names a backing's return annotation may reference but import only under
    ``TYPE_CHECKING`` (the cores/surfaces). Built lazily -- the query layer must
    not import the cores at module load. Mirrors ``scripts.gen_stubs._NS``."""
    global _NS
    if _NS is None:
        from ..clients import WaitConfig
        from ..collection import Collection, Field
        from ..core.client import WebClient
        from ..core.document import Document, Element
        from ..core.reference import HttpMethod, Reference
        from ..core.document.models import Flag, Metadata, Signal, Structure, Transport

        _NS = {
            "Reference": Reference,
            "Document": Document,
            "WebClient": WebClient,
            "Element": Element,
            "Field": Field,
            "Collection": Collection,
            "HttpMethod": HttpMethod,
            "Transport": Transport,
            "Metadata": Metadata,
            "Structure": Structure,
            "Signal": Signal,
            "Flag": Flag,
            "WaitConfig": WaitConfig,
            "Any": Any,
        }
    return _NS


def safe_type_check(value: Any, hint: Any) -> bool:
    """Whether ``value`` satisfies ``hint`` (``typeguard.check_type``). Used only
    to choose the matching overload from the arg values the recorder was called
    with -- lenient: a hint that is absent / ``Any`` / an unresolved forward-ref
    string, or one typeguard cannot evaluate, never rejects."""
    if hint is Any or hint is inspect.Parameter.empty or hint is None:
        return True
    if isinstance(hint, str):  # an unresolved forward ref -- do not reject
        return True
    try:
        check_type(value, hint)
        return True
    except TypeCheckError:
        return False
    except Exception:  # a hint typeguard cannot evaluate -- be lenient
        return True


def _user_params(func: Any) -> inspect.Signature:
    """``func``'s signature with the leading ``self``/``core`` receiver params
    dropped (backing ops are ``def op(self, core, ...)``); the recorder binds only
    the user-facing args."""
    sig = inspect.signature(func)
    params = list(sig.parameters.values())
    drop = 0
    for p in params[:2]:
        if p.name in ("self", "core"):
            drop += 1
    return sig.replace(parameters=params[drop:])


def _hints(func: Any) -> dict[str, Any]:
    try:
        return typing.get_type_hints(func, localns=_ns())
    except Exception:  # a forward ref we cannot resolve here -> raw strings
        return dict(getattr(func, "__annotations__", {}))


def resolve_hints(
    func: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    """The resolved type hints of the first overload of ``func`` whose params
    bind and type-check against ``args``/``kwargs`` (mirrors a static checker's
    first-match). Falls back to ``func`` itself when it has no overloads."""
    for f in typing.get_overloads(func) or [func]:
        try:
            bound = _user_params(f).bind(*args, **kwargs)
        except TypeError:
            continue  # this overload's params do not accept these args
        hints = _hints(f)
        if all(
            name not in hints or safe_type_check(value, hints[name])
            for name, value in bound.arguments.items()
        ):
            return hints
    raise TypeError(
        f"no overload of {getattr(func, '__qualname__', func)!r} matches "
        f"args={args!r} kwargs={kwargs!r}"
    )


def _op_method(core_cls: type, op: str) -> Any:
    """The backing method that provides ``op`` for ``core_cls`` (the richest
    signature wins, so a shared op shows its full form) -- ``None`` if no backing
    provides it. Mirrors ``scripts.gen_stubs._provider``."""
    cands = [
        b
        for b in getattr(core_cls, "BACKINGS", ())
        if op in b.provides or op in b.props
    ]
    if not cands:
        return None
    best = max(
        cands, key=lambda b: len(inspect.signature(getattr(type(b), op)).parameters)
    )
    return getattr(type(best), op)


def _field_annotation(core_cls: type, name: str) -> Any:
    """A pydantic Core data field's resolved annotation, else ``_MISSING``."""
    field = getattr(core_cls, "model_fields", {}).get(name)
    return field.annotation if field is not None else _MISSING


def _resolve_self(annotation: Any, self_type: Any) -> Any:
    """Substitute ``Self`` with ``self_type`` (the type resolving the annotation),
    including inside a generic like ``list[Self]``."""
    if annotation is typing.Self:
        return self_type
    origin = typing.get_origin(annotation)
    if origin is not None:
        new_args = tuple(
            _resolve_self(a, self_type) for a in typing.get_args(annotation)
        )
        try:
            return annotation.copy_with(new_args)  # typing special forms
        except Exception:
            try:
                return origin[new_args]
            except Exception:
                return annotation
    return annotation


def return_type(
    core_cls: type,
    op: str,
    args: tuple[Any, ...] = (),
    kwargs: dict[str, Any] | None = None,
    *,
    self_type: Any = None,
) -> Any:
    """The resolved return annotation for ``core_cls.op(*args, **kwargs)`` -- read
    from the provider backing's matching overload, or from a core data field's
    annotation, with ``Self`` bound to ``self_type`` (default ``core_cls``). The
    raw annotation (a ``Core`` type / ``Field[T]`` / ``list[...]`` / scalar) is
    returned; the recorder maps it to its tier's surface class."""
    kwargs = {} if kwargs is None else kwargs
    self_type = self_type if self_type is not None else core_cls
    func = _op_method(core_cls, op)
    if func is not None:  # a backing call/prop op -> its matching overload
        annotation = resolve_hints(func, args, kwargs).get("return", Any)
        return _resolve_self(annotation, self_type)
    prop = inspect.getattr_static(core_cls, op, None)  # a class @property (ok/url)
    if isinstance(prop, property) and prop.fget is not None:
        annotation = _hints(prop.fget).get("return", Any)
        return _resolve_self(annotation, self_type)
    ann = _field_annotation(core_cls, op)  # a pydantic data field
    if ann is not _MISSING:
        return _resolve_self(ann, self_type)
    raise AttributeError(f"{core_cls.__name__} has no op, property or field {op!r}")


#: ``attr_return_type`` sentinel: accessing this name yields a *callable* op whose
#: return type is only known once the call args are seen (resolve with
#: ``return_type`` on ``__call__``).
CALL_OP: Any = object()


def attr_return_type(core_cls: type, name: str) -> Any:
    """What accessing ``core_cls.name`` yields, for a recorder's ``__getattr__``:
    ``CALL_OP`` when ``name`` is a backing *call* op (its return depends on the
    call args -- resolve later with :func:`return_type`), otherwise the resolved
    value type of a property op / class ``@property`` / data field (or
    ``AttributeError`` if ``name`` is none of these)."""
    if any(name in b.provides for b in getattr(core_cls, "BACKINGS", ())):
        return CALL_OP
    return return_type(core_cls, name)


__all__ = [
    "safe_type_check",
    "resolve_hints",
    "return_type",
    "attr_return_type",
    "CALL_OP",
]
