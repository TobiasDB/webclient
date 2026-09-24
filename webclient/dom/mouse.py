"""Human-like mouse paths -- ONE generator for the live driver and the replay.

The browser tier moves the pointer along :func:`human_mouse_path` before every click
(``LiveBacking``), and the Player draws the same curve when it replays the action: the path
is a pure function of the endpoints and a seed derived from them, so nothing has to be
recorded or shipped -- both sides compute it. The TypeScript port (``packages/ui/src/lib/
mouse.ts``) uses the same integer PRNG and the same arithmetic, point for point.

The shape: a cubic Bézier whose two control points sit off the straight line (a bow of 10-25 %
of the distance, one side or the other), sampled with ease-in-out timing (slow start, fast
middle, a settle at the end -- more points near the target), plus a sub-pixel jitter on the
interior points. Duration follows Fitts's law in spirit: longer moves take longer, but never
long enough to feel like an animation.

    xs, ys = zip(*human_mouse_path(100, 100, 640, 400))
"""

from __future__ import annotations

import math

__all__ = ["human_mouse_path", "mouse_duration_ms", "path_timings_ms", "path_seed"]


def path_seed(x1: float, y1: float, x2: float, y2: float) -> int:
    """A stable seed from the endpoints (rounded to the pixel) -- what makes the driver's
    move and the replay's drawing the same curve."""
    h = 2166136261
    for v in (round(x1), round(y1), round(x2), round(y2)):
        h ^= (int(v) + 0x7FFF) & 0xFFFFFFFF
        h = (h * 16777619) & 0xFFFFFFFF
    return h or 1


def _xorshift32(seed: int):  # type: ignore[no-untyped-def]
    """A tiny PRNG identical in Python and TypeScript (32-bit xorshift); yields floats in [0, 1)."""
    s = seed & 0xFFFFFFFF or 1

    def nxt() -> float:
        nonlocal s
        s ^= (s << 13) & 0xFFFFFFFF
        s ^= s >> 17
        s ^= (s << 5) & 0xFFFFFFFF
        return (s & 0xFFFFFFFF) / 4294967296.0

    return nxt


def human_mouse_path(
    x1: float, y1: float, x2: float, y2: float, resolution: int | None = None, seed: int | None = None
) -> list[tuple[float, float]]:
    """The points of a human-like move from (x1, y1) to (x2, y2): ``resolution`` points
    (default from the distance: 8 - 48), the first exactly the start, the last exactly the
    end. ``seed`` (default :func:`path_seed`) fixes the bow and the jitter."""
    dx, dy = x2 - x1, y2 - y1
    dist = math.hypot(dx, dy)
    n = resolution if resolution is not None else max(8, min(48, int(dist / 14) + 8))
    if dist < 1e-6:
        return [(x1, y1)] * max(2, min(n, 2))
    rnd = _xorshift32(seed if seed is not None else path_seed(x1, y1, x2, y2))
    # a bow to one side, bigger for longer moves; the two control points bend the same way
    side = 1.0 if rnd() < 0.5 else -1.0
    bow = dist * (0.10 + 0.15 * rnd()) * side
    nx, ny = -dy / dist, dx / dist  # the unit normal
    c1 = (x1 + dx * (0.25 + 0.15 * rnd()) + nx * bow, y1 + dy * 0.3 + ny * bow)
    c2 = (x1 + dx * (0.65 + 0.15 * rnd()) + nx * bow * 0.6, y1 + dy * 0.7 + ny * bow * 0.6)
    pts: list[tuple[float, float]] = []
    for i in range(n):
        u = i / (n - 1)
        t = u * u * (3 - 2 * u)  # ease in-out: slow off the mark, a settle at the end
        mt = 1 - t
        bx = mt ** 3 * x1 + 3 * mt * mt * t * c1[0] + 3 * mt * t * t * c2[0] + t ** 3 * x2
        by = mt ** 3 * y1 + 3 * mt * mt * t * c1[1] + 3 * mt * t * t * c2[1] + t ** 3 * y2
        if 0 < i < n - 1:  # sub-pixel tremor on the way, none at the endpoints
            bx += (rnd() - 0.5) * 1.2
            by += (rnd() - 0.5) * 1.2
        pts.append((round(bx, 2), round(by, 2)))
    pts[0] = (float(x1), float(y1))
    pts[-1] = (float(x2), float(y2))
    return pts


def mouse_duration_ms(distance: float) -> float:
    """How long a human takes to move ``distance`` pixels (Fitts-shaped: 120 ms + 90 ms per
    doubling past 50 px, capped at 700 ms)."""
    return min(700.0, 120.0 + 90.0 * math.log2(1.0 + max(0.0, distance) / 50.0))


def path_timings_ms(n: int, duration_ms: float) -> list[float]:
    """The time offset of each of ``n`` points over ``duration_ms``, eased the same way the
    points are spaced (so the pointer decelerates onto the target)."""
    if n <= 1:
        return [0.0]
    return [round(duration_ms * (u * u * (3 - 2 * u)), 1) for u in (i / (n - 1) for i in range(n))]
