"""``wrap``: present a dispatch result as an eager value.

A resolved core IS its own eager surface -- ``WebCore.__getattr__`` dispatches its
ops -- so there is no wrapper class. ``wrap`` only turns a list of cores into a
``Collection`` (so the row-shaping ops apply); a single core is already its
surface and a ``Field``/scalar passes through. Kept as the name the executor and
renderer import; it delegates to ``core.web_core._wrap_result``.
"""

from __future__ import annotations

from ..core.web_core import _wrap_result as wrap

__all__ = ["wrap"]
