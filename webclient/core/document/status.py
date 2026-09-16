"""StatusBacking: status / value ops, available even on a not-ok document."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...collection import Field
from ..web_core import Backing
from .models import PageCard

if TYPE_CHECKING:
    from ..reference import Reference
    from . import Document


class StatusBacking(Backing):
    """Status / value ops, available even on a not-ok document: ``is_ok`` /
    ``is_empty`` (a ``Field``), ``message`` (the error text), and ``card`` (a lean
    self-descriptor -- the default crawl projection)."""

    provides = frozenset({"is_ok", "is_empty", "ref", "reload", "card"})
    props = frozenset({"message"})
    io = frozenset({"reload"})  # re-resolves -> awaitable under async
    gate = "ok"

    def applies(self, core: "Document") -> bool:
        return True

    def card(self, core: "Document") -> "PageCard":
        """A lean :class:`PageCard` descriptor of this page (url / kind / title /
        description / flags / the tier it was fetched at) -- enough to rebuild a
        Reference. Works on any kind (facets it lacks are just omitted); it is the
        default crawl projection (``project=doc.card()``), a serializable expression
        that runs local or remote alike."""
        t = core.dispatch("transport") if core.has_op("transport") else None
        return PageCard(
            url=core.url,
            final_url=core.final_url,
            kind=core.kind,
            status_code=core.status_code,
            title=core.dispatch("title") if core.has_op("title") else None,
            description=(
                core.dispatch("metadata").description if core.has_op("metadata") else None
            ),
            flags=[f.name for f in core.dispatch("flags")] if core.has_op("flags") else [],
            final_tier=t.final_tier if t is not None else "static",
            escalation=t.escalation if t is not None else ["static"],
        )

    def ref(self, core: "Document") -> "Reference | None":
        """The reference that produced this document (for reload / recovery)."""
        return core._ref

    async def reload(self, core: "Document") -> "Document":
        """Re-resolve on a fresh page, replaying the recorded action chain --
        available even after the page was released. An IO op: the interface
        bridges it (``dispatch``)."""
        return await core._client.areload(core)

    def is_ok(self, core: "Document") -> "Field[bool]":
        return Field(core.ok)

    def is_empty(self, core: "Document") -> "Field[bool]":
        empty = core._missing or not core.ok or not (core.content or core._element)
        return Field(bool(empty))

    def message(self, core: "Document") -> str:
        return core.error.message if core.error is not None else ""


__all__ = ["StatusBacking"]
