"""Alias shim: this module moved to ``webclient.kernel.models`` (the package split).

Kept so existing ``from ..models import X`` / ``webclient.models`` imports keep working
while call sites migrate to ``webclient.kernel.models``; delete once they have.
"""

from .kernel.models import *  # noqa: F401,F403
from .kernel.models import NetworkViewEvent as NetworkViewEvent  # noqa: F401  (not in __all__)
