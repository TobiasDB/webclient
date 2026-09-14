"""StatusBacking: status / value ops, available even on a not-ok document."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from ...collection import Field
from ..web_core import Backing

if TYPE_CHECKING:
    from ..reference import ReferenceCore
    from . import DocumentCore


class StatusBacking(Backing):
    """Status / value ops, available even on a not-ok document: ``is_ok`` /
    ``is_empty`` (a ``Field``), ``message`` (the error text)."""

    provides = frozenset({"is_ok", "is_empty", "ref", "summary", "reload"})
    props = frozenset({"message"})
    gate = "ok"

    def applies(self, core: "DocumentCore") -> bool:
        return True

    def ref(self, core: "DocumentCore") -> "ReferenceCore | None":
        """The reference that produced this document (for reload / recovery)."""
        return cast("ReferenceCore | None", core._ref)

    def reload(self, core: "DocumentCore") -> "DocumentCore":
        """Re-resolve on a fresh page, replaying the recorded action chain --
        available even after the page was released."""
        return cast(
            "DocumentCore", core._client.loop().run(core._client._areload(core))
        )

    def summary(self, core: "DocumentCore") -> dict[str, Any]:
        """A page digest: url / ok, plus title + markdown when available."""
        out: dict[str, Any] = {"url": core.final_url or core.url, "ok": core.ok}
        if core.has_op("title"):
            out["title"] = core.dispatch("title")
        if core.has_op("render"):
            out["markdown"] = core.dispatch("render", "markdown")
        return out

    def is_ok(self, core: "DocumentCore") -> "Field[bool]":
        return Field(core.ok)

    def is_empty(self, core: "DocumentCore") -> "Field[bool]":
        empty = core._missing or not core.ok or not (core.content or core._element)
        return Field(bool(empty))

    def message(self, core: "DocumentCore") -> str:
        return core.error.message if core.error is not None else ""


__all__ = ["StatusBacking"]
