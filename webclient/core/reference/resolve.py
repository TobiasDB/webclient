"""ResolveBacking: resolve the reference into a document via the bound client."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from ..web_core import Backing

if TYPE_CHECKING:
    from ..document_core import DocumentCore
    from . import ReferenceCore


class ResolveBacking(Backing):
    """Resolve the reference into a document via the bound client."""

    provides = frozenset({"resolve"})
    gate = "ok"

    def resolve(
        self,
        core: "ReferenceCore",
        *,
        browser: bool = False,
        optional: bool = False,
        error: Any = None,
    ) -> "DocumentCore":
        from ...errors import RETURN

        lenient = optional or error is RETURN
        target = core._session or core._client
        if target is None:
            from ..client_core import WebClientCore

            target = WebClientCore()  # process-local default
        coro = target.afetch(core, optional=lenient, browser=browser)
        loop = target.loop()
        # On the engine loop (the async executor) hand back the coroutine to
        # await; off it (a sync caller) bridge onto the loop.
        return cast("DocumentCore", coro if loop.on_loop_thread() else loop.run(coro))
