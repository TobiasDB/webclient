"""The BROWSER layer of web.fetch, gathered in one package.

Everything that drives a real browser lives here, separated from the plain-HTTP transports:

  - :mod:`.backend`  -- :class:`BrowserFetcher` (the ``fetch``/``session`` backend) and
    :class:`BrowserSession` (a live, owned page you interact with).
  - :mod:`.chrome`   -- the browser SUPPLY: launch a local process vs attach a remote Chrome over
    CDP, plus binary resolution (:func:`real_chrome_path`, :func:`ensure_chromium`).
  - :mod:`.script`   -- the page scripts the backend injects (DOM recorder, deep-DOM shadow/frame
    inlining) and the :class:`ScriptRegistry`.
  - :mod:`.wait`     -- browser readiness (``apply_wait`` / dom-stable).
  - :mod:`.mouse`    -- human-like cursor paths for a page that scores pointer behaviour.

The rest of ``web.fetch`` (and the outside world) reaches these through this package facade, so
the split is internal; the public ``web.fetch.<name>`` imports are unchanged.
"""

from __future__ import annotations

from .backend import BrowserFetcher, BrowserSession
from .chrome import (
    BrowserSupply,
    CdpSupply,
    LaunchSupply,
    ensure_chromium,
    real_chrome_path,
    supply_for,
)
from .script import DEEP_DOM, DOM_RECORDER, OPEN_SHADOW, ScriptRegistry, default_scripts
from .wait import apply_wait

__all__ = [
    "BrowserFetcher",
    "BrowserSession",
    "BrowserSupply",
    "LaunchSupply",
    "CdpSupply",
    "supply_for",
    "real_chrome_path",
    "ensure_chromium",
    "ScriptRegistry",
    "default_scripts",
    "DOM_RECORDER",
    "OPEN_SHADOW",
    "DEEP_DOM",
    "apply_wait",
]
