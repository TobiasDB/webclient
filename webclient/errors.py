"""Alias shim: this module moved to ``webclient.kernel.errors`` (the package split).

Kept so existing ``from ..errors import X`` / ``webclient.errors`` imports keep working
while call sites migrate to ``webclient.kernel.errors``; delete once they have. Re-exports
the whole public surface plus the two names imported cross-module that are not in
``__all__`` (``_Policy``, ``lenient``).
"""

from .kernel.errors import *  # noqa: F401,F403
from .kernel.errors import _Policy as _Policy, lenient as lenient  # noqa: F401
