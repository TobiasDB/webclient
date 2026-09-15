"""ResolveBacking: resolve the reference into a document via the bound client."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from ..web_core import Backing

if TYPE_CHECKING:
    from ...clients import WaitConfig
    from ..document import Document
    from . import Reference


class ResolveBacking(Backing):
    """Resolve the reference into a document via the bound client."""

    provides = frozenset({"resolve"})
    io = frozenset({"resolve"})  # an IO op: the interface bridges it (dispatch)
    gate = "ok"

    async def resolve(
        self,
        core: "Reference",
        *,
        browser: "bool | Literal['never', 'auto', 'always']" = False,
        optional: bool = False,
        error: Any = None,
        keep_alive: "bool | float" = False,
        wait: "WaitConfig | None" = None,
    ) -> "Document":
        """Resolve into a document. ``keep_alive`` (browser only) marks the live page
        as caller-owned so a plan won't auto-release it -- release it yourself with
        ``release(doc)``, or pass a number of seconds for a TTL auto-release. ``wait``
        (a :class:`~webclient.clients.WaitConfig`, browser only) picks the render wait
        strategy + timeout behaviour."""
        from ...errors import lenient

        target = core._session or core._client  # bound (a default by _bridge_io)
        return await target.afetch(
            core,
            optional=lenient(optional, error),
            browser=browser,
            keep_alive=keep_alive,
            wait=wait,
        )
