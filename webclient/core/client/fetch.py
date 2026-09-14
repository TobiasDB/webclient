"""FetchBacking: the client's authoring verbs (``ref``/``lazy``/``fetch``/
``summary``) -- eager and real like every backing op, returning real cores/values.
It is the *surface* that is lazy: the client records these calls into a plan and
the executor dispatches them here at run time (the IO ops hand back a coroutine
when already on the engine loop, bridged otherwise -- like ``Reference.resolve``)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from ..document import Document
from ..reference import HttpMethod, Reference, from_url
from ..web_core import Backing

if TYPE_CHECKING:
    from ...summary import Summary
    from . import WebClient


class FetchBacking(Backing):
    """The client's authoring verbs -- eager and real like every backing op,
    returning real cores/values (``ref -> Reference``, ``fetch ->
    Document``, ``summary -> dict``). It is the *surface* that is lazy: the
    client records these calls into a plan and the executor dispatches them here
    at run time (the IO ops hand back a coroutine when already on the engine
    loop, bridged otherwise -- like ``Reference.resolve``)."""

    provides = frozenset({"ref", "fetch", "summary"})
    io = frozenset({"fetch", "summary"})  # resolve+project cross the IO bridge
    gate = "ok"

    def ref(
        self, core: "WebClient", url: Any, method: str = "get", **kw: Any
    ) -> "Reference":
        """A client-bound reference. ``url`` may be a URL string, a ``Reference``
        surface, or a ``Reference``."""
        spec = getattr(url, "_core", url)  # unwrap a Reference surface
        if not isinstance(spec, Reference):
            spec = from_url(url, cast(HttpMethod, method), **kw)
        spec._client = core
        return spec

    async def fetch(
        self,
        core: "WebClient",
        url: Any,
        *,
        optional: bool = False,
        error: Any = None,
        **kw: Any,
    ) -> "Document":
        """Resolve ``ref(url)`` into a document -- straight to the client's
        transport (``afetch``), not bouncing back out through the reference's
        ``resolve`` op (``fetch`` IS a resolve). An IO op: the interface bridges
        it (``dispatch``)."""
        from ...errors import lenient

        ref = self.ref(core, url, **kw)
        return await core.afetch(ref, optional=lenient(optional, error))

    async def summary(
        self, core: "WebClient", url: Any, *include: str, **kw: Any
    ) -> "Summary":
        """Resolve ``url`` and project it to a :class:`Summary`. ``include``
        selects facets (default: all applicable)."""
        ref = self.ref(core, url, **kw)
        doc = await core.afetch(ref)
        return doc.summary(*include)  # typed: Document implements its ops


__all__ = ["FetchBacking"]
