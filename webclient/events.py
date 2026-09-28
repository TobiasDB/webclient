"""Alias shim: this module moved to ``webclient.kernel.events`` (the package split).

Kept so existing ``from ..events import X`` / ``webclient.events`` imports keep working
while call sites migrate to ``webclient.kernel.events``; delete once they have. Re-exports
the public surface plus the run-scope context vars used cross-module (not in ``__all__``).
"""

from .kernel.events import *  # noqa: F401,F403
from .kernel.events import (  # noqa: F401  (context vars imported cross-module, not in __all__)
    CURRENT_ITEM as CURRENT_ITEM,
    CURRENT_PLAN as CURRENT_PLAN,
    CURRENT_RUN as CURRENT_RUN,
    CURRENT_STEP as CURRENT_STEP,
    run_scope as run_scope,
)
