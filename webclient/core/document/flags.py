"""FlagsBacking: the ``flags`` facet -- the typed Document surface over the detection
registry in :mod:`webclient.signals`.

This backing is thin on purpose: it builds a :class:`~webclient.signals.Context` from
the document (its response + the captured browser tree/events) and asks the registry
for the flags. All detection logic -- which signals feed which flag, at which stage,
with what confidence -- lives in :mod:`webclient.signals` (registry-driven and
extensible). The per-flag ops here are just the typed accessors the generated surface
needs; adding a flag to the SURFACE is one op + a regen, but the detection itself is
extended by registering a detector, no change here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...signals import Context, flags as detect_flags, framework as detect_framework
from ...signals import dom as _dom  # noqa: F401  (registers the rendered/tree detectors)
from ..web_core import Backing
from .html import tree
from .models import Flag, XhrCall

if TYPE_CHECKING:
    from . import Document


class FlagsBacking(Backing):
    """The ``flags`` facet: per-flag ops returning a :class:`Flag` (spa /
    anti_bot_present / anti_bot_triggered / login_present / login_required /
    pagination / forms / buttons), plus a ``flags()`` digest and the
    ``framework``/``xhr_endpoints`` data ops. A total facet (always applies)."""

    provides = frozenset(
        {"flags", "spa", "anti_bot_present", "anti_bot_triggered", "login_present",
         "login_required", "pagination", "forms", "buttons", "framework", "xhr_endpoints"}
    )
    gate = "ok"

    def applies(self, core: "Document") -> bool:
        return True

    # -- the detection context + the flag set ---------------------------------
    def _context(self, core: "Document") -> Context:
        """A detection Context from the document: its response plus, for an html/xml
        document, the parsed tree and any captured browser DOM/network events."""
        root = None
        if core.kind in ("html", "xml"):
            try:
                root = tree(core)
            except Exception:
                root = None
        chain = [core.url, core.final_url] if core.final_url and core.final_url != core.url else [core.url]
        return Context.from_response(
            core.status_code, core.response_headers, core._set_cookies, core.content, chain,
            url=core.url, final_url=core.final_url or core.url,
            tree=root, events=core._events, render_stats=core._render_stats,
        )

    def _flags(self, core: "Document") -> "dict[str, Flag]":
        return detect_flags(self._context(core))

    # -- per-flag typed accessors ---------------------------------------------
    def spa(self, core: "Document") -> Flag:
        """The page builds its content client-side. ``value`` is the same-origin XHR
        endpoints -- an agent can hit the data API instead of scraping the SPA."""
        return self._flags(core)["spa"]

    def anti_bot_present(self, core: "Document") -> Flag:
        """A bot-management vendor is in front of the site (a fingerprint), whether or
        not it is challenging us now. ``value`` is the vendor name."""
        return self._flags(core)["anti_bot_present"]

    def anti_bot_triggered(self, core: "Document") -> Flag:
        """The site is actively challenging/blocking this request -- ``remedy``
        ``stealth`` for a named vendor, ``proxy`` for a bare block."""
        return self._flags(core)["anti_bot_triggered"]

    def login_present(self, core: "Document") -> Flag:
        """A login form exists on the page (not necessarily a wall)."""
        return self._flags(core)["login_present"]

    def login_required(self, core: "Document") -> Flag:
        """A login wall blocks the content (no transport remedy -- needs credentials)."""
        return self._flags(core)["login_required"]

    def pagination(self, core: "Document") -> Flag:
        """The dataset spans multiple pages. ``value`` notes the next-page pattern."""
        return self._flags(core)["pagination"]

    def forms(self, core: "Document") -> Flag:
        """Interactive forms on the page. ``value`` is the form list (method / action
        / field names)."""
        return self._flags(core)["forms"]

    def buttons(self, core: "Document") -> Flag:
        """Interactive buttons on the page. ``value`` is a sample of button labels."""
        return self._flags(core)["buttons"]

    # -- the digest + data ops ------------------------------------------------
    def flags(self, core: "Document") -> "list[Flag]":
        """Every flag that is present, most-actionable first -- the compact digest of
        "what is notable about this page"."""
        order = (
            "anti_bot_triggered", "login_required", "spa", "anti_bot_present",
            "login_present", "pagination", "forms", "buttons",
        )
        got = self._flags(core)
        return [got[name] for name in order if got[name].present]

    def framework(self, core: "Document") -> "str | None":
        """The detected JS framework name (from markers in the served HTML), or None."""
        return detect_framework((core.content or b"").decode(core.encoding or "utf-8", "replace"))

    def xhr_endpoints(self, core: "Document") -> "list[XhrCall]":
        """The XHR/fetch calls a browser render captured -- an SPA's real data sources."""
        from ...models import NetworkEvent

        return [
            XhrCall(
                method=str(e.request.method).upper() if e.request is not None else "GET",
                url=str(e.request.dispatch("url")) if e.request is not None else "",
            )
            for e in core._events
            if isinstance(e, NetworkEvent) and e.resource_type in ("xhr", "fetch")
        ]


__all__ = ["FlagsBacking"]
