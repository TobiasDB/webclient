"""RETIRED: web patterns are now Signals/Flags, not a parallel registry.

The pattern detectors live in :mod:`webclient.signals.patterns` (``@detector``s feeding the
``record_regions`` / ``repeated_controls`` / ``page_template`` flags); read them via
``doc.patterns(for_=...)`` or the per-flag accessors ``doc.record_regions()`` /
``doc.repeated_controls()`` / ``doc.page_template()``. :class:`PatternHint` is a flag value model in
:mod:`webclient.core.document.models`. This module keeps only compatibility re-exports; the old
``@pattern`` / ``PATTERNS`` / ``PatternContext`` / ``detect`` registry has been removed.
"""

from __future__ import annotations

from .core.document.models import PatternHint
from .signals.patterns import template_signature

__all__ = ["PatternHint", "template_signature"]
