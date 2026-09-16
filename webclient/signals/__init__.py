"""Signal & flag detection -- the isolated, extensible home for reading a resolved
response into the flags callers act on (spa / anti-bot / login / pagination / …).

Two-layer model:
  * a **Signal** is one piece of evidence, tagged with a ``stage`` (request / static
    / rendered / network) and a ``confidence``;
  * a **Flag** is the conclusion, built from its signals (confidence = noisy-OR).

Detection is a **registry** of small functions (:mod:`.registry`): to add a signal,
write a ``@detector`` function; to add a flag, ``flag(...)`` plus its detectors --
nothing else changes. The pure request/static detectors (:mod:`.request_static`) are
registered on import (so ``auto`` reads them on the first hop, and a remote resolve
agrees); the facet adds the rendered/network + tree detectors (:mod:`.dom`).

``Signal`` / ``Flag`` themselves live in :mod:`webclient.core.document.models` (the
facet's typed return values); this package imports them lazily to avoid a cycle.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from . import request_static as _request_static  # noqa: F401  (registers pure detectors)
from .context import Context
from .registry import (
    DETECTORS,
    FLAGS,
    Detector,
    FlagSpec,
    Hit,
    build_flag,
    detector,
    flag,
    flags,
    run,
)
from .request_static import _framework

if TYPE_CHECKING:
    from ..core.document.models import Flag


def framework(html: str) -> "str | None":
    """The JS framework named by a marker in the served HTML, or ``None``."""
    return _framework(html)


def flags_from_response(
    status: int,
    headers: "Mapping[Any, Any] | Iterable[tuple[Any, Any]]",
    cookies: "Iterable[str] | Mapping[str, Any]",
    body: "bytes | str | None",
    redirect_chain: "Iterable[str]" = (),
    **facet: Any,
) -> "dict[str, Flag]":
    """The flags of a raw response -- build a :class:`Context` and run the registry.
    Pure over request/static inputs (what ``auto`` reads); pass ``tree`` / ``events``
    / ``render_stats`` (the facet does) to also fire the rendered/network detectors."""
    return flags(Context.from_response(status, headers, cookies, body, redirect_chain, **facet))


__all__ = [
    "Context",
    "Hit",
    "Detector",
    "FlagSpec",
    "detector",
    "flag",
    "build_flag",
    "run",
    "flags",
    "flags_from_response",
    "framework",
    "DETECTORS",
    "FLAGS",
]
