"""Session: an alias for ``WebClient``.

A WebClient IS a session -- it holds state/config for interacting with the web, its
lifecycle + name-scope + engine-sharing come from :class:`~..session_core.SessionCore`,
and its identity (headers + a cookie jar, when ``keep_cookies``) lives on the client
itself. ``wc.session()`` opens a CHILD ``WebClient`` (a nested scope on the same engine)
with its own identity + ttl. So there is no separate ``Session`` class -- this name is
kept as an alias for the cores/tests/surfaces that still refer to it.
"""

from __future__ import annotations

from ..client import WebClient

Session = WebClient

__all__ = ["Session"]
