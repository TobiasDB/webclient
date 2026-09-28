"""Human-like mouse movement -- siloed in fetch, used by a live click when ``human=True``.

A straight teleport of the cursor is a bot tell; a real pointer follows a curved path at varying
speed. :func:`human_path` samples a quadratic Bezier with a randomised control point into a list
of points, and :func:`move_along` drives a Playwright mouse through them. Kept isolated: it is a
self-contained anti-detection helper, not part of the core fetch contract.
"""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page


def human_path(x1: float, y1: float, x2: float, y2: float, *, steps: int = 24) -> "list[tuple[float, float]]":
    """A curved cursor path from (x1,y1) to (x2,y2): a quadratic Bezier through one jittered
    control point, sampled into ``steps`` points."""
    cx = (x1 + x2) / 2 + random.uniform(-1, 1) * abs(x2 - x1) * 0.3
    cy = (y1 + y2) / 2 + random.uniform(-1, 1) * abs(y2 - y1) * 0.3
    out: list[tuple[float, float]] = []
    for i in range(1, steps + 1):
        t = i / steps
        u = 1 - t
        x = u * u * x1 + 2 * u * t * cx + t * t * x2
        y = u * u * y1 + 2 * u * t * cy + t * t * y2
        out.append((x, y))
    return out


async def move_along(page: "Page", x: float, y: float, *, steps: int = 24) -> None:
    """Move ``page``'s mouse from its current spot to (x,y) along a human path, with easing pauses.
    Starts from the viewport centre (Playwright does not expose the current cursor position)."""
    vp = page.viewport_size or {"width": 1280, "height": 800}
    for px, py in human_path(vp["width"] / 2, vp["height"] / 2, x, y, steps=steps):
        await page.mouse.move(px, py)
        await asyncio.sleep(random.uniform(0.004, 0.016))


__all__ = ["human_path", "move_along"]
