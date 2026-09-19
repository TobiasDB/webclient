"""State fingerprints for recorded plan steps -- ADVISORY drift / sequence divergence.

A fingerprint is a LIGHTWEIGHT, STRUCTURAL signature of a page's state -- NOT an exact
content hash, and never a gate. It is a digest of the page's element count plus its sorted
tag-name histogram, so it is deterministic across renders (a clean replay matches) yet
tolerant of content/data changes (dates, prices, ids) while catching STRUCTURAL divergence
-- a section added or removed, an interaction that no longer reveals its content. Computed
off the same live DOM the correlation substrate stamps; region-scoping and tolerance are
later refinements. See :meth:`WebClient.record` (capture) and the executor's ``step`` replay
(compare).
"""

from __future__ import annotations

import hashlib
from typing import Any

#: a structural signature of the live DOM: total element count + a sorted tag-name
#: histogram (structure only -- no text, ids, or attribute values), so data churn does
#: not move it but a structural change does.
_FP_JS = """
(function () {
  var els = document.getElementsByTagName('*'), h = {};
  for (var i = 0; i < els.length; i++) { var t = els[i].tagName; h[t] = (h[t] || 0) + 1; }
  return els.length + '|' + Object.keys(h).sort().map(function (k) { return k + h[k]; }).join(',');
})()
"""


def _digest(signature: str) -> str:
    """A short, stable hex digest of a structural signature."""
    return hashlib.blake2b(signature.encode("utf-8", "replace"), digest_size=8).hexdigest()


async def page_fingerprint(page: Any) -> str:
    """The structural fingerprint of a live page's current DOM (best-effort -- ``""`` if
    the page cannot be read; a fingerprint is advisory, so a failure never propagates)."""
    try:
        signature = await page.evaluate(_FP_JS)
    except Exception:  # noqa: BLE001 - advisory: a fingerprint never breaks a run
        return ""
    return _digest(str(signature))


async def fingerprint(doc: Any) -> str:
    """The structural fingerprint of ``doc``'s reached state. Uses the live page when the
    document holds one (a recorded/replayed interaction step always does); otherwise ``""``
    (a static document has no interactive state to drift)."""
    page = getattr(doc, "_page", None)
    return await page_fingerprint(page) if page is not None else ""


__all__ = ["fingerprint", "page_fingerprint"]
