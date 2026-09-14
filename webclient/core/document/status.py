"""StatusBacking: status / value ops, available even on a not-ok document."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from ...collection import Field
from ..web_core import Backing

if TYPE_CHECKING:
    from ..reference import ReferenceCore
    from . import DocumentCore


class StatusBacking(Backing):
    """Status / value ops, available even on a not-ok document: ``is_ok`` /
    ``is_empty`` (a ``Field``), ``message`` (the error text)."""

    provides = frozenset({"is_ok", "is_empty", "ref", "reload"})
    props = frozenset({"message"})
    io = frozenset({"reload"})  # re-resolves -> awaitable under async
    gate = "ok"

    def applies(self, core: "DocumentCore") -> bool:
        return True

    def ref(self, core: "DocumentCore") -> "ReferenceCore | None":
        """The reference that produced this document (for reload / recovery)."""
        return cast("ReferenceCore | None", core._ref)

    async def reload(self, core: "DocumentCore") -> "DocumentCore":
        """Re-resolve on a fresh page, replaying the recorded action chain --
        available even after the page was released. An IO op: the interface
        bridges it (``dispatch``)."""
        return await core._client._areload(core)

    def is_ok(self, core: "DocumentCore") -> "Field[bool]":
        return Field(core.ok)

    def is_empty(self, core: "DocumentCore") -> "Field[bool]":
        empty = core._missing or not core.ok or not (core.content or core._element)
        return Field(bool(empty))

    def message(self, core: "DocumentCore") -> str:
        return core.error.message if core.error is not None else ""


__all__ = ["StatusBacking"]
