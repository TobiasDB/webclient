"""WebCore: the shared core machinery -- Capabilities + Dispatch + Backing +
Choose (rewrite foundation).

Every core (client / session / document / reference) is a ``WebCore``: it holds
a set of ``Backing``s, CHOOSES which apply to its current state, and DISPATCHES
an op to the first chosen backing that ``provides`` it. Its capabilities are
the union of the chosen backings' gates. This generalises the (clean) backing
dispatch that ``DocumentCore`` used, so all four cores share one mechanism.

The cores carry data ("Core Fields") and talk to each other; the user-facing
surface (``LazyDocument`` / ``Document`` / ...) is GENERATED from a core's
fields + its backings' ops (see ``webclient.gen``), never hand-written.
"""
from __future__ import annotations

from typing import Any, ClassVar


class UnsupportedOp(TypeError):
    """An op no chosen backing provides (the receiver lacks the capability)."""

    def __init__(self, op: str, have: frozenset[str]) -> None:
        super().__init__(
            f"{op!r} is not available here; this core has "
            f"{sorted(have) or 'no capabilities'}")
        self.op, self.have = op, have


class Backing:
    """Serves a family of ops for one medium. ``provides`` names the ops it
    implements (each a ``method(self, core, *args, **kwargs)``); ``gate`` is the
    capability it grants when chosen; ``applies`` decides if it is in play for a
    given core's current state (default: always)."""

    provides: ClassVar[frozenset[str]] = frozenset()
    gate: ClassVar[str] = "ok"

    def applies(self, core: "WebCore") -> bool:
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
        """The first chosen backing that provides ``op`` (else ``UnsupportedOp``)."""
        for b in self.choose():
            if op in b.provides:
                return b
        raise UnsupportedOp(op, self.capabilities())

    def dispatch(self, op: str, *args: Any, **kwargs: Any) -> Any:
        """Run ``op`` on its backing, passing this core as the receiver."""
        return getattr(self.backing(op), op)(self, *args, **kwargs)

    def provides(self, op: str) -> bool:
        """Whether any chosen backing provides ``op`` (without raising)."""
        return any(op in b.provides for b in self.choose())

    # -- op surface (for generation) ----------------------------------------
    @classmethod
    def ops(cls) -> dict[str, Backing]:
        """Every op this core can dispatch, op-name -> the backing that owns
        it (first wins). The generator reads this (+ the Core Fields) to build
        the Lazy / eager surface classes."""
        table: dict[str, Backing] = {}
        for backing in cls.BACKINGS:
            for op in backing.provides:
                table.setdefault(op, backing)
        return table


__all__ = ["WebCore", "Backing", "UnsupportedOp"]
