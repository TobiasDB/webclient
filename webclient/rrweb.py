"""rrweb DOM recording (roadmap N3 step B, decision D2): instead of growing our own DOM
serialiser, the vendored rrweb ``record()`` runs in the page as a named script
(``wc.rrweb``) and its events -- a full snapshot at load, then node-id-keyed incremental
mutations, input, scroll and viewport events -- are drained into :class:`RRWebEvent`
chunks on the bus -- ordinary events in the one trace stream. :mod:`webclient.replay.rrweb`
is the translation layer: the whole stream (recorded chunks, snapshots synthesised for static
runs, and every other event as an rrweb *custom* event) becomes one rrweb event list that
``rrweb-player`` drives, and an rrweb recording maps back into our events.

OFF by default (the bundle is ~140 KB injected per page): it is enabled only while a trace
is active (``with wc.trace(...)``), unless a caller registers/enables ``wc.rrweb`` itself.
rrweb is MIT-licensed; the bundle is ``webclient/scripts_js/rrweb.min.js`` (2.0.0-alpha.4).
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources
from typing import Any

from .models import Event

__all__ = ["RRWebEvent", "RRWEB_VERSION", "VIEWPORT", "init_source", "DRAIN_SOURCE", "SCRIPT_NAME"]

RRWEB_VERSION = "2.0.0-alpha.4"
SCRIPT_NAME = "wc.rrweb"
#: ONE viewport for every replay: the browser tier renders at it (the default identity's
#: viewport), a static run's synthesised snapshot declares it, so the player never resizes
#: between documents.
VIEWPORT = (1280, 800)


class RRWebEvent(Event):
    """A chunk of rrweb events drained from the page (``events`` is rrweb's own JSON: each
    ``{type, data, timestamp}``; type 2 = FullSnapshot, 3 = IncrementalSnapshot, 4 = Meta)."""

    topic: str = "rrweb"
    events: list[dict[str, Any]] = []
    count: int = 0


@lru_cache(maxsize=1)
def bundle() -> str:
    """The vendored rrweb bundle source."""
    return resources.files("webclient").joinpath("scripts_js/rrweb.min.js").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def init_source() -> str:
    """The ``init`` script: load rrweb and start recording into ``window.__wc_rrweb``
    (bounded so a runaway page cannot exhaust memory; the drain resets it)."""
    return bundle() + """
;(() => {
  if (window.__wc_rrweb_on) return;
  window.__wc_rrweb_on = true;
  window.__wc_rrweb = [];
  try {
    rrweb.record({
      emit(e) { if (window.__wc_rrweb.length < 20000) window.__wc_rrweb.push(e); },
      recordCanvas: false, collectFonts: false, inlineStylesheet: true,
      sampling: { mousemove: false, mouseInteraction: true, scroll: 100, input: 'last' },
    });
  } catch (e) { window.__wc_rrweb_error = String(e); }
})();"""


#: the ``drain`` script: pull (and reset) the recorded events.
DRAIN_SOURCE = "() => { const e = window.__wc_rrweb || []; window.__wc_rrweb = []; return e; }"


def chunk(events: Any, *, document_id: "str | None") -> "RRWebEvent | None":
    """Wrap a drained list into an :class:`RRWebEvent` (``None`` when empty)."""
    if not isinstance(events, list) or not events:
        return None
    return RRWebEvent(events=events, count=len(events), document_id=document_id, source="rrweb")
