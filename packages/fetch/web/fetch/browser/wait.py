"""``Wait`` -- how the browser backend decides a page is ready before it snapshots.

A served HTML page is ready at ``load``; a JS/SPA page rewrites its own DOM for a while after
that, so snapshotting at ``load`` captures an empty shell. ``Wait`` makes the readiness milestone
explicit and bounded: pick the one that matches how the page delivers content. Settling milestones
(``networkidle`` / ``dom_stable``) treat hitting the budget as a normal settle and return what has
rendered; a concrete condition (``selector``) that never arrives raises, so a real miss is loud.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..models import Wait

if TYPE_CHECKING:
    from playwright.async_api import Page


async def apply_wait(page: "Page", wait: Wait) -> None:
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


async def _dom_stable(page: "Page", timeout: float, quiet: float) -> None:
    """Return once the DOM node count stops changing for ``quiet`` seconds, or the budget is hit
    (a settle, NEVER a failure) -- the safe general wait for a page that rewrites its own DOM. A
    settling wait must not raise: if the page navigates / redirects / is challenged mid-wait, the JS
    execution context is destroyed and ``page.evaluate`` throws -- that is still a settle (return what
    has rendered), not a transport error. Hitting the budget is a settle too.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last, stable_since = -1, None
    while loop.time() < deadline:
        try:
            n = int(await page.evaluate("document.getElementsByTagName('*').length"))
        except Exception:
            return  # execution context gone (navigation / challenge / redirect) -> settle, not raise
        if n == last:
            stable_since = stable_since or loop.time()
            if loop.time() - stable_since >= quiet:
                return
        else:
            last, stable_since = n, None
        await asyncio.sleep(0.1)


__all__ = ["apply_wait"]
