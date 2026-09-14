"""The Summary schema -- a compact, deterministic, token-lean overview of a
resolved Document.

The models now live in :mod:`webclient.models` (the shared, dependency-light data
models); this module re-exports them so ``from webclient.summary import Summary``
keeps working. The facets are assembled by ``webclient.core.document.summary``.
"""

from __future__ import annotations

from .models import (
    FACETS,
    Form,
    Metadata,
    Probe,
    Runtime,
    Structure,
    Summary,
    TocEntry,
    Transport,
    XhrCall,
)

__all__ = [
    "Summary",
    "Transport",
    "Metadata",
    "Structure",
    "Runtime",
    "Probe",
    "TocEntry",
    "Form",
    "XhrCall",
    "FACETS",
]
