"""webclient -- declarative web client (ground-up rewrite in progress).

Surface objects are generated from the Cores' fields + their backings' typed
ops (``webclient.gen``); the Cores + backings hold all behaviour, ``Expr``
records plans, ``typeinfo`` resolves types.
"""
from .surfaces import Reference, from_url

__all__ = ["Reference", "from_url"]
