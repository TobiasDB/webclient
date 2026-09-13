"""Expr: the lazy recorder -- a simple, Core-agnostic external tool.

An ``Expr`` chains attribute access and calls into a tree and tracks the
*result type* (``root``) of each step via ``typeinfo`` (best-effort -- the hard
edge cases are noted there). It knows nothing about Document/Core; those seed a
root ``Expr`` and replay it. At runtime every surface value is an ``Expr``; the
static type is the generated Lazy/eager stub it is cast to.
"""
from __future__ import annotations

from typing import Any

from . import typeinfo


class ExprMeta(type):
    """Metaclass for ``Expr`` -- the static/runtime seam (TODO): present a root
    ``Expr`` as its ``root`` type to the checker / register valid roots. Minimal
    by design; recording needs no metaclass, only the typing does."""


class Expr(metaclass=ExprMeta):
    """A recorded step: ``parent``, the tracked result type ``root``, the
    ``op`` (``__getattr__`` / ``__call__`` / operator), and the args."""

    __slots__ = ("parent", "root", "op", "args", "kwargs")

    def __init__(self, parent: "Expr | None", root: Any, op: str,
                 *args: Any, **kwargs: Any) -> None:
        self.parent = parent
        self.root = root
        self.op = op
        self.args = args
        self.kwargs = kwargs

    def __getattr__(self, name: str) -> "Expr":
        if name.startswith("_"):                  # the one safety boundary
            raise AttributeError(name)
        return Expr(self, _member_type(self.root, name), "__getattr__", name)

    def __call__(self, *args: Any, **kwargs: Any) -> "Expr":
        # ``root`` was set to the target member by the preceding __getattr__;
        # a call resolves to its return type (best-effort).
        return Expr(self, _resolve_call(self.root), "__call__", *args, **kwargs)

    # TODO: operator ops (__eq__/.. -> a bool-typed Expr) and the single
    #   evaluation trigger (collect/execute) handing the tree to a core.


def _member_type(root: Any, name: str) -> Any:
    """Best-effort type of ``root.name``: a Core data field, else an op (keep
    the method so __call__ can read its return), else ``Any``."""
    if isinstance(root, type) and name in getattr(root, "model_fields", {}):
        return typeinfo.field_type(root, name)
    fn = getattr(root, name, None)
    return fn if callable(fn) else Any


def _resolve_call(member: Any) -> Any:
    """The return type of calling ``member`` (best-effort)."""
    return typeinfo.return_type(member) if callable(member) else member


def root(cls: type) -> Any:
    """A root ``Expr`` for ``cls`` (statically ``cls``, at runtime an Expr)."""
    return Expr(None, cls, "root")


__all__ = ["Expr", "ExprMeta", "root"]
