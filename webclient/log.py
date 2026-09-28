"""Alias shim: this module moved to ``webclient.kernel.log`` (the package split).

Kept so existing ``from ..log import X`` / ``webclient.log`` imports keep working while call
sites migrate to ``webclient.kernel.log``; delete once they have.
"""

from .kernel.log import *  # noqa: F401,F403
