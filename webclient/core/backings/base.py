"""Backing base + shared helpers (PLAN §5b/§5d).

A Backing serves a family of ops for one medium; ``DocumentCore.dispatch``
routes an op to the first backing that ``provides`` it. Each medium lives in
its own module (html / json / live); ``__init__`` holds the switch.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..document import _is_xpath
from ..base import Capability

if TYPE_CHECKING:
    from ..document import DocumentCore


def pw_selector(selector: str) -> str:
    return f"xpath={selector}" if _is_xpath(selector) else selector


def _timeout_ms(timeout: float | None, default: float) -> float:
    return (timeout if timeout is not None else default) * 1000


class Backing:
    """Serves ``provides`` for one medium; ``gate`` is the capability a
    Document needs for this backing to be chosen (used in error messages)."""

    provides: ClassVar[frozenset[str]] = frozenset()
    gate: ClassVar[Capability] = "ok"
