"""FetchBacking: the client's authoring verbs (``ref``/``fetch``) -- eager and real
like every backing op, returning real cores/values. It is the *surface* that is
lazy: the client records these calls into a plan and the executor dispatches them
here at run time (the IO ops hand back a coroutine when already on the engine loop,
bridged otherwise -- like ``Reference.resolve``)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, cast

from ..document import Document
from ..reference import HttpMethod, Reference, from_url
from ..web_core import Backing

if TYPE_CHECKING:
    from . import WebClient


class FetchBacking(Backing):
    """The client's authoring verbs -- eager and real like every backing op,
    returning real cores/values (``ref -> Reference``, ``fetch -> Document``). It is
    the *surface* that is lazy: the client records these calls into a plan and the
    executor dispatches them here at run time (the IO ops hand back a coroutine when
    already on the engine loop, bridged otherwise -- like ``Reference.resolve``)."""

    provides = frozenset({"ref", "fetch"})
    io = frozenset({"fetch"})  # resolve crosses the IO bridge
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
        browser: "bool | Literal['never', 'auto', 'always', 'probe']" = False,
        optional: bool = False,
        error: Any = None,
        keep_alive: "bool | float" = False,
        **kw: Any,
    ) -> "Document":
        """Resolve ``ref(url)`` into a document -- straight to the client's
        transport (``afetch``), not bouncing back out through the reference's
        ``resolve`` op (``fetch`` IS a resolve). ``browser`` picks the tier
        (``False`` static / ``"auto"`` escalate-if-JS-gated / ``True`` always /
        ``"probe"`` resolve both and compare -- the explicit "do I need a browser"
        diagnostic). ``keep_alive`` (browser only) marks the live page caller-owned
        so a plan won't auto-release it (a number gives a TTL). An IO op: the
        interface bridges it (``dispatch``)."""
        from ...errors import lenient

        ref = self.ref(core, url, **kw)
        return await core.afetch(
            ref, optional=lenient(optional, error), browser=browser, keep_alive=keep_alive
        )


__all__ = ["FetchBacking"]
