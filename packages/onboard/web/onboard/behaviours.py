"""Behaviours -- the Signals/Flags registry that MODIFIES Author's query / interpretation.

Where a :mod:`.patterns` rule chooses the query SHAPE, a **Behaviour** is keyed on a
detection flag/signal and tweaks it: a ``record_list`` should drop rows whose key field is empty; a
``paginated`` list needs a paginating resolver; a ``consent_wall`` or ``tabbed`` region needs an
interaction before the records are in the DOM. Each is a :class:`Behaviour` (an optional query
modifier + an advisory note); registering another is one ``@behaviour`` / ``register_behaviour``.

Some behaviours change the query directly (a ``wq`` ``.filter(...)``); others can only ADVISE
(pagination / interactions are resolver- or browser-side, not expressible in a static plan), so
they carry a ``note`` Author returns alongside the query.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from web.dsl import LazyCollection, wq

from .models import DatasetBrief, Reference

#: a query modifier sees the recorded collection plus the reference/brief context.
_Modify = Callable[[LazyCollection, Reference, DatasetBrief], LazyCollection]


@dataclass(frozen=True)
class Behaviour:
    """A flag/signal -> a query modifier and/or an advisory note. ``modify`` reshapes the query
    (``None`` = advice only); ``note`` is guidance Author surfaces (a pager / interaction needed)."""

    flag: str
    note: str = ""
    modify: "_Modify | None" = None


_BEHAVIOURS: list[Behaviour] = []


def register_behaviour(b: Behaviour) -> Behaviour:
    """Add a Behaviour to the registry (extensible; no if-chain to edit)."""
    _BEHAVIOURS.append(b)
    return b


def behaviour(flag: str, note: str = "") -> "Callable[[_Modify], _Modify]":
    """Register a query-MODIFYING behaviour for ``flag`` (decorates the modifier)."""
    def deco(fn: _Modify) -> _Modify:
        register_behaviour(Behaviour(flag, note, fn))
        return fn
    return deco


@behaviour("record_list")
def _drop_empty_rows(q: LazyCollection, reference: Reference, brief: DatasetBrief) -> LazyCollection:
    """A detected record region often includes blank scaffolding siblings -- drop rows whose first
    requested field came back empty, so the dataset is the records, not the frame."""
    if brief.fields:
        return q.filter(wq.field(brief.fields[0]) != "")
    return q


# advisory-only behaviours: the remedy is resolver- or browser-side, not a static-plan change.
register_behaviour(Behaviour("paginated", note="the listing is paginated -- resolve with a "
                             "paginating profile (paginate_links / paginate_param) to span pages"))
register_behaviour(Behaviour("infinite_scroll", note="the listing grows on scroll -- use a "
                             "browser profile with a scroll/Load-more loop (paginate_clicks)"))
register_behaviour(Behaviour("consent_wall", note="a consent banner may overlay the content -- "
                             "dismiss it first (a browser interaction) before reading"))
register_behaviour(Behaviour("tabbed", note="some records sit behind tabs that populate on click "
                             "-- drive each tab (a browser interaction) to capture them all"))
register_behaviour(Behaviour("iframe", note="the content sits in an iframe -- descend into the "
                             "framed source (a browser render inlines it)"))


def apply_behaviours(q: LazyCollection, reference: Reference, brief: DatasetBrief) -> "tuple[LazyCollection, list[str]]":
    """Apply every registered behaviour whose flag/signal fired on ``reference``: run its query
    modifier (if any) and collect its note. Returns the modified query and the advisory notes."""
    fired = set(reference.flags) | set(reference.signals)
    notes: list[str] = []
    for b in _BEHAVIOURS:
        if b.flag in fired:
            if b.modify is not None:
                q = b.modify(q, reference, brief)
            if b.note:
                notes.append(b.note)
    return q, notes


__all__ = ["Behaviour", "behaviour", "register_behaviour", "apply_behaviours"]
