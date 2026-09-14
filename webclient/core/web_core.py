"""WebCore: the shared core machinery -- Capabilities + Dispatch + Backing +
Choose (rewrite foundation).

Every core (client / session / document / reference) is a ``WebCore``: it holds
a set of ``Backing``s, CHOOSES which apply to its current state, and DISPATCHES
an op to the first chosen backing that ``provides`` it. Its capabilities are
the union of the chosen backings' gates. This generalises the (clean) backing
dispatch that ``DocumentCore`` used, so all four cores share one mechanism.

The cores carry data ("Core Fields") and talk to each other; the user-facing
surface (``LazyDocument`` / ``Document`` / ...) is GENERATED from a core's
fields + its backings' ops (see ``scripts.gen_stubs``), never hand-written.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Self


class UnsupportedOp(TypeError):
    """An op no chosen backing provides (the receiver lacks the capability)."""

    def __init__(self, op: str, have: frozenset[str]) -> None:
        super().__init__(
            f"{op!r} is not available here; this core has "
            f"{sorted(have) or 'no capabilities'}"
        )
        self.op, self.have = op, have


class Backing:
    """Serves a family of ops for one medium. ``provides`` names the ops it
    implements (each a ``method(self, core, *args, **kwargs)``); ``gate`` is the
    capability it grants when chosen; ``applies`` decides if it is in play for a
    given core's current state (default: always)."""

    provides: ClassVar[frozenset[str]] = frozenset()  # call ops: obj.op(...)
    props: ClassVar[frozenset[str]] = frozenset()  # property ops: obj.op
    gate: ClassVar[str] = "ok"

    def applies(self, core: Any) -> bool:  # Any: subclasses narrow to their core
        return True


class WebCore:
    """Capabilities + Dispatch + Backing + Choose.

    Subclasses declare their backings in ``BACKINGS`` (and their Core Fields as
    attributes / a pydantic model -- TBD in the rewrite). Everything a core can
    *do* comes from a backing and is reached through ``dispatch``; the cores
    themselves hold state and wiring, not op logic."""

    #: the backings this kind of core can use, in priority order (first that
    #: provides an op wins).
    BACKINGS: ClassVar[tuple[Backing, ...]] = ()

    # -- choose / capabilities ----------------------------------------------
    def choose(self) -> list[Backing]:
        """The backings that apply to this core's current state, in order."""
        return [b for b in self.BACKINGS if b.applies(self)]

    def capabilities(self) -> frozenset[str]:
        """The union of the chosen backings' gates."""
        return frozenset(b.gate for b in self.choose())

    # -- dispatch ------------------------------------------------------------
    def backing(self, op: str) -> Backing:
        """The first chosen backing that provides ``op`` (call or prop) else
        ``UnsupportedOp``."""
        for b in self.choose():
            if op in b.provides or op in b.props:
                return b
        raise UnsupportedOp(op, self.capabilities())

    def dispatch(self, op: str, *args: Any, **kwargs: Any) -> Any:
        """Run ``op`` on its backing, passing this core as the receiver."""
        return getattr(self.backing(op), op)(self, *args, **kwargs)

    def has_op(self, op: str) -> bool:
        """Whether any chosen backing provides ``op`` (call or prop)."""
        return any(op in b.provides or op in b.props for b in self.choose())

    # -- realization: an eager value is already realised -----------------------
    def collect(self, context: Any = None) -> "Self":
        """An eager surface is already materialised, so ``collect`` is identity
        (the lazy recorder's ``collect`` runs the plan; this is the eager twin so
        the same ``x.collect()`` works whether ``x`` is eager or lazy)."""
        return self

    async def acollect(self, context: Any = None) -> "Self":
        return self

    @property
    def lazy(self) -> Any:
        """A lazy recorder rooted at this surface (bound to it so
        ``doc.lazy.select(...).collect()`` records then runs against this core).
        The generated surface stubs re-type this as the matching ``Lazy`` variant
        (``wc.lazy`` -> ``Lazy[WebClient]``, ``doc.lazy`` -> ``LazyDocument``...)."""
        from ..query.expr import lazy_root

        return lazy_root(self)

    # -- a core IS its own eager surface -------------------------------------
    if not TYPE_CHECKING:  # hidden from type checkers -- the eager surface stubs
        # (``Document``/``Reference``) are the typed interface; a bare
        # ``__getattr__`` here would make every attribute access ``Any``.

        def __getattr__(self, name: str) -> Any:
            """A resolved core is directly usable as its eager surface: an op name
            dispatches immediately (a prop op returns its value; a call op returns
            a dispatcher), a list of cores comes back as a ``Collection``. Non-op
            names delegate to the next ``__getattr__`` in the MRO -- pydantic's,
            for the cores' private attrs (``_page``/``_client``/...)."""
            if not name.startswith("_"):
                cls = type(self)
                if name in cls.prop_ops():
                    return _wrap_result(self.dispatch(name))
                if name in cls.ops():

                    def _call(*args: Any, **kwargs: Any) -> Any:
                        return _wrap_result(self.dispatch(name, *args, **kwargs))

                    return _call
            from pydantic import BaseModel

            # delegate to pydantic's __getattr__ (private attrs); it is a runtime
            # method not in the type stubs, so fetch it dynamically.
            pyd_getattr = getattr(BaseModel, "__getattr__", None)
            if pyd_getattr is not None:
                return pyd_getattr(self, name)
            raise AttributeError(name)

    # -- op surface (for generation) ----------------------------------------
    @classmethod
    def ops(cls) -> dict[str, Backing]:
        """Every *call* op, op-name -> owning backing (first wins)."""
        table: dict[str, Backing] = {}
        for backing in cls.BACKINGS:
            for op in backing.provides:
                table.setdefault(op, backing)
        return table

    @classmethod
    def prop_ops(cls) -> dict[str, Backing]:
        """Every *property* op, name -> owning backing (first wins)."""
        table: dict[str, Backing] = {}
        for backing in cls.BACKINGS:
            for op in backing.props:
                table.setdefault(op, backing)
        return table


def _wrap_result(value: Any) -> Any:
    """Present a dispatch result as an eager value: a list of cores becomes a
    ``Collection`` (so the row-shaping ops apply); a single core is already its
    own surface; anything else (a ``Field``/scalar) passes through."""
    if isinstance(value, (list, tuple)) and any(isinstance(v, WebCore) for v in value):
        from ..collection import Collection

        owner = getattr(value[0], "_client", None)
        root = getattr(value[0], "root", "") or getattr(value[0], "name", "")
        return Collection(list(value), client=owner, root=root)
    return value


__all__ = ["WebCore", "Backing", "UnsupportedOp"]
