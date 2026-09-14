"""Shared reference helpers: the HTTP-method type, default ports, and the
derivation copy helper. Imported by both the package ``__init__`` (which defines
``ReferenceCore``/``from_url``) and the ``derive`` backing, with no import cycle
-- nothing here references ``ReferenceCore`` at runtime (annotations only)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from . import ReferenceCore

HttpMethod = Literal["get", "post", "put", "patch", "delete", "head", "options"]
DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}


def _derive(original: "ReferenceCore", copy: "ReferenceCore") -> "ReferenceCore":
    """A derived reference: unnamed, rooted at the original, and with a fresh
    surface slot (model_copy carries private attrs, else the stale surface)."""
    copy._surface = None
    copy.name = ""
    copy.root = original.name or original.root
    return copy
