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
        policy: "dict[str, Any] | None" = None,
        optional: bool = False,
        error: Any = None,
        keep_alive: "bool | float" = False,
        wait: "WaitConfig | None" = None,
    ) -> "Document":
        """Resolve into a document. ``policy`` bakes the FULL fetch policy (proxy / antibot /
        browser / rate / retry -- a serialised :class:`Resolve`) into the step, so a stored
        blob re-fetches with the SAME policy it was authored under (a proxy/anti-bot source
        does not silently re-fetch un-proxied); the browser tier is taken from it. ``browser``
        alone still works for the common case. ``keep_alive`` (browser only) marks the live
        page as caller-owned so a plan won't auto-release it. ``wait`` picks the render wait."""
        from ...errors import lenient
        from .models import Resolve

        pol: "Resolve | None" = None
        if policy is not None:
            pol = Resolve(**policy)
            if pol.browser is not None:  # the tier travels inside the policy
                browser = pol.browser.when
        target = core._session or core._client  # bound (a default by _bridge_io)
        return await target.afetch(
            core,
            optional=lenient(optional, error),
            browser=browser,
            resolve=pol,
            keep_alive=keep_alive,
            wait=wait,
        )
