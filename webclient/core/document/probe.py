"""ProbeBacking: the ``probe`` facet -- what the resolution had to escalate to
(browser / proxy / anti-bot / a paywall or login wall it hit), projected from the
``ProbeRecord`` the transport ladder wrote onto the document. Only applies once a
probe was recorded; a plain static fetch leaves the facet unavailable."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..web_core import Backing
from .models import Probe

if TYPE_CHECKING:
    from . import Document


class ProbeBacking(Backing):
    """The ``probe`` facet: what the resolution escalated to, from the recorded
    ``ProbeRecord``."""

    provides = frozenset({"probe"})
    gate = "probe"

    def applies(self, core: "Document") -> bool:
        return core._probe is not None

    def probe(self, core: "Document") -> Probe:
        r = core._probe
        # keep it lean: report only the flags that are true (None otherwise).
        return Probe(
            was_browser_required=r.was_browser_required or None,
            was_proxy_required=r.was_proxy_required or None,
            anti_bot=r.anti_bot,
            js_required=r.js_required or None,
            paywall=r.paywall or None,
            login_wall=r.login_wall or None,
            render_blocked=r.render_blocked or None,
            render_gain=r.render_gain,
        )


__all__ = ["ProbeBacking"]
