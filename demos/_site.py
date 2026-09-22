"""The demos' offline site is the package LAB (``webclient.lab``): one fixture per feature,
each publishing its expected result. This shim keeps ``from _site import serve, h1, kv,
table`` working and maps the old page paths onto the lab's (``/`` -> ``/lab/shop`` etc.).

Pages (the lab's): /lab/shop (a shop listing), /lab/shop/items/<n> (JSON), /lab/spa (a
JS-gated page), /lab/feed (+ /lab/feed/api/items), /lab/login, /lab/app (a live cart) --
and many more: see ``python -m webclient.lab`` then ``/lab``.
"""

from __future__ import annotations

from typing import Any

from webclient.lab import serve as _serve


def serve() -> str:
    """Start the lab (once per process) and return its base URL; pages live under ``/lab``."""
    return _serve()


# -- pretty output ---------------------------------------------------------- #
def h1(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m\n" + "─" * len(title))


def kv(label: str, value: Any) -> None:
    print(f"  {label:<14} {value}")


def table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("  (no rows)")
        return
    cols = list(rows[0].keys())
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    print("  " + "  ".join(c.ljust(widths[c]) for c in cols))
    print("  " + "  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  " + "  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))
