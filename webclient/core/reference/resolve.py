"""ResolveBacking: resolve the reference into a document via the bound client."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..web_core import Backing

if TYPE_CHECKING:
    from ..document import DocumentCore
    from . import ReferenceCore


class ResolveBacking(Backing):
    """Resolve the reference into a document via the bound client."""

    provides = frozenset({"resolve"})
    io = frozenset({"resolve"})  # an IO op: the interface bridges it (dispatch)
    gate = "ok"

    async def resolve(
        self,
        core: "ReferenceCore",
        *,
        browser: bool = False,
        optional: bool = False,
        error: Any = None,
    ) -> "DocumentCore":
        from ...errors import RETURN

        lenient = optional or error is RETURN
        target = core._session or core._client  # bound (a default by _bridge_io)
        return await target.afetch(core, optional=lenient, browser=browser)
