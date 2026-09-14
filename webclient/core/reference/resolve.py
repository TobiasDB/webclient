"""ResolveBacking: resolve the reference into a document via the bound client."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..web_core import Backing

if TYPE_CHECKING:
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
        browser: bool = False,
        optional: bool = False,
        error: Any = None,
    ) -> "Document":
        from ...errors import lenient

        target = core._session or core._client  # bound (a default by _bridge_io)
        return await target.afetch(
            core, optional=lenient(optional, error), browser=browser
        )
