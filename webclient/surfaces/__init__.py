"""The typed surfaces.

Split into three modules (one runtime behind all of them, generated stubs on top):
- :mod:`._base` -- the ``Surface`` runtime base + ``wrap`` / ``@surface`` registry.
- :mod:`.eager` -- the eager surfaces (``Document`` / ``Reference`` /
  ``WebClient`` / ``AsyncWebClient`` / ``Session`` / ``RemoteWebClient`` / ...).
- :mod:`.lazy` -- the lazy recorder tier (``LazyDocument`` / ``LazyReference`` /
  ``LazyField`` / ``LazyCollection``).

Re-exported here so ``from webclient.surfaces import X`` reaches every surface.
"""

from ._base import Eager, Surface, surface, wrap
from .eager import (
    AsyncWebClient,
    Document,
    LiveDocument,
    Reference,
    RemoteWebClient,
    Renderer,
    Session,
    WebClient,
    default_client,
    from_url,
)
from .lazy import Lazy, LazyCollection, LazyDocument, LazyField, LazyReference

__all__ = [
    "Eager",
    "Surface",
    "surface",
    "wrap",
    "Reference",
    "Document",
    "LiveDocument",
    "Renderer",
    "Session",
    "WebClient",
    "AsyncWebClient",
    "RemoteWebClient",
    "default_client",
    "from_url",
    "Lazy",
    "LazyField",
    "LazyReference",
    "LazyDocument",
    "LazyCollection",
]
