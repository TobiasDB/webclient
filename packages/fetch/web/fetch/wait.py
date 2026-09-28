"""``Wait`` -- how the browser backend decides a page is ready before it snapshots.

A served HTML page is ready at ``load``; a JS/SPA page rewrites its own DOM for a while after
that, so snapshotting at ``load`` captures an empty shell. ``Wait`` makes the readiness milestone
explicit and bounded: pick the one that matches how the page delivers content. Settling milestones
(``networkidle`` / ``dom_stable``) treat hitting the budget as a normal settle and return what has
rendered; a concrete condition (``selector``) that never arrives raises, so a real miss is loud.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from pydantic import BaseModel

#: the readiness milestones (see the class docstring).
Until = Literal["domcontentloaded", "load", "networkidle", "dom_stable", "selector"]


class Wait(BaseModel):
    """When to snapshot. ``timeout`` bounds the whole wait; ``quiet`` is the settle window for
    ``dom_stable`` (how long the DOM node count must hold); ``selector`` targets ``until='selector'``."""

    until: Until = "load"
    timeout: float = 8.0
    quiet: float = 0.4
    selector: "str | None" = None


async def apply_wait(page: Any, wait: Wait) -> None:
    """Wait for ``wait``'s milestone on an already-navigated ``page``. ``load`` is handled by the
    initial ``goto``, so it is a no-op here."""
    ms = int(wait.timeout * 1000)
    if wait.until in ("domcontentloaded", "load"):
        return  # the nav milestone is handled by goto's wait_until; nothing more to settle
    if wait.until == "networkidle":
        try:  # many sites never truly idle -> a timeout here is a settle, not a failure
            await page.wait_for_load_state("networkidle", timeout=ms)
        except Exception:
            pass
    if wait.until == "selector" and wait.selector:
        await page.wait_for_selector(wait.selector, timeout=ms)  # a real miss raises (loud)
    elif wait.until == "dom_stable":
        await _dom_stable(page, wait.timeout, wait.quiet)


async def _dom_stable(page: Any, timeout: float, quiet: float) -> None:
    """Return once the DOM node count stops changing for ``quiet`` seconds, or the budget is hit
    (a settle, not a failure) -- the safe general wait for a page that rewrites its own DOM."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last, stable_since = -1, None
    while loop.time() < deadline:
        n = await page.evaluate("document.getElementsByTagName('*').length")
        if n == last:
            stable_since = stable_since or loop.time()
            if loop.time() - stable_since >= quiet:
                return
        else:
            last, stable_since = n, None
        await asyncio.sleep(0.1)


__all__ = ["Wait", "apply_wait", "Until"]
