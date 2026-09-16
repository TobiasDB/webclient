"""Resiliency: the ``Resolve`` policies declared to a proxy service as request
headers (:func:`policy_headers`). Response DETECTION -- the signals + flags the
``auto`` ladder acts on -- lives in its own package, :mod:`webclient.signals`."""

from __future__ import annotations

from .headers import policy_headers

__all__ = ["policy_headers"]
