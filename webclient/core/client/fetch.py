"""FetchBacking: the client's authoring verbs (``ref``/``lazy``/``fetch``/
``summary``) -- eager and real like every backing op, returning real cores/values.
It is the *surface* that is lazy: the client records these calls into a plan and
the executor dispatches them here at run time (the IO ops hand back a coroutine
when already on the engine loop, bridged otherwise -- like ``Reference.resolve``)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from ..document import DocumentCore
from ..reference import HttpMethod, ReferenceCore, from_url
from ..web_core import Backing

if TYPE_CHECKING:
    from ...summary import Summary
    from . import WebClientCore


class FetchBacking(Backing):
    """The client's authoring verbs -- eager and real like every backing op,
    returning real cores/values (``ref -> ReferenceCore``, ``fetch ->
    DocumentCore``, ``summary -> dict``). It is the *surface* that is lazy: the
    client records these calls into a plan and the executor dispatches them here
    at run time (the IO ops hand back a coroutine when already on the engine
    loop, bridged otherwise -- like ``ReferenceCore.resolve``)."""

    provides = frozenset({"ref", "fetch", "summary"})
    io = frozenset({"fetch", "summary"})  # resolve+project cross the IO bridge
    gate = "ok"

    def ref(
        self, core: "WebClientCore", url: Any, method: str = "get", **kw: Any
    ) -> "ReferenceCore":
        """A client-bound reference. ``url`` may be a URL string, a ``Reference``
        surface, or a ``ReferenceCore``."""
        spec = getattr(url, "_core", url)  # unwrap a Reference surface
        if not isinstance(spec, ReferenceCore):
            spec = from_url(url, cast(HttpMethod, method), **kw)
        spec._client = core
        return spec

    def fetch(
        self,
        core: "WebClientCore",
        url: Any,
        *,
        optional: bool = False,
        error: Any = None,
        **kw: Any,
    ) -> "DocumentCore":
        """Resolve ``ref(url)`` into a document (via the reference's resolve op,
        so it is async-aware on the engine loop)."""
        ref = self.ref(core, url, **kw)
        return cast(
            DocumentCore, ref.dispatch("resolve", optional=optional, error=error)
        )

    def summary(
        self, core: "WebClientCore", url: Any, *include: str, **kw: Any
    ) -> "Summary":
        """Resolve ``url`` and project it to a :class:`Summary` (async-aware).
        ``include`` selects facets (default: all applicable)."""
        ref = self.ref(core, url, **kw)

        async def run() -> "Summary":
            doc = await core.afetch(ref)
            return cast("Summary", doc.dispatch("summary", *include))

        return cast("Summary", core.bridge(run()))


__all__ = ["FetchBacking"]
