"""onboarding.reference -- see the package docstring."""


from typing import Sequence


#: the pipeline's logger. Stages log progress here (seeds, crawl, candidates, the
#: evaluation, the query, spend); a CLI or app sets the level / handler. Each line is
#: also appended to ``OnboardingResult.steps`` for a programmatic trace.

from ...core.document.models import Flag
from ...policy import (
    AntiBotPolicy,
    BrowserPolicy,
    ProxyPolicy,
    Resolve,
)
from ...interface import Reference, WebClient

from .artifacts import CandidateEval


# --------------------------------------------------------------------------- #
# 5. write_reference  (deterministic given the candidate)
# --------------------------------------------------------------------------- #


def _source_url(evaluation: CandidateEval) -> str:
    """The URL to root the reference + query at. Prefer an observed data API (``api_endpoint``)
    over the page ONLY when the page is a client-rendered SHELL (the ``spa`` flag fired) whose
    records come from that API. A directly-scrapable page -- data already in the served/rendered
    HTML -- is queried as the PAGE ITSELF even if it also fired an XHR (a secondary fetch,
    analytics, or a model mis-pick), so we never reference an XHR when the page was the right
    source (the query was authored + tested against this URL)."""
    if evaluation.api_endpoint and evaluation.flags.get("spa"):
        return evaluation.api_endpoint
    return evaluation.url


def write_reference(evaluation: CandidateEval, *, wc: WebClient) -> Reference:
    """The lazy ``Reference`` for the chosen source -- deterministic given the candidate. Rooted
    at the page URL, or at a same-origin data API only when the page is an SPA shell backed by it
    (see :func:`_source_url`)."""
    return wc.ref(_source_url(evaluation))


# --------------------------------------------------------------------------- #
# 6. write_resolve  (deterministic given the flags)
# --------------------------------------------------------------------------- #


def write_resolve(flags: Sequence[Flag], *, needs_browser: bool = False) -> Resolve:
    """The ``Resolve`` policy for the source -- deterministic from its flags. A
    ``spa`` needs a browser render; an ``anti_bot_triggered`` needs its remedy
    (``proxy`` for a bare block, ``stealth`` = a browser behind a proxy with anti-bot
    handling for a named vendor). A login wall has no transport remedy. ``needs_browser``
    is set when the source could ONLY be FETCHED via a browser (a static UA is blocked,
    e.g. a 403 with no SPA/anti-bot flag -- Wikipedia): the browser tier must then be baked
    into the shipped blob, or ``from_blob(...).collect()`` re-fetches statically and gets 0 rows."""
    from ...core.client.resolve_loop import RENDER_FLAGS

    by = {f.name: f for f in flags if f.present}
    # a browser is needed to build the DOM: an SPA composes it client-side; shadow DOM /
    # a same-origin iframe hides content a plain HTML snapshot misses, and only a render
    # inlines it (see the __wc_inline page script). RENDER_FLAGS is the ONE shared vocabulary
    # the resolve ladder also reads -- here (a confirmed source) we bake a browser for ANY of them.
    needs_render = any(n in by for n in RENDER_FLAGS)
    triggered = by.get("anti_bot_triggered")
    stealth = bool(triggered and triggered.remedy == "stealth")
    proxy = bool(triggered and triggered.remedy in ("proxy", "stealth"))
    return Resolve(
        browser=BrowserPolicy(when="always") if (needs_render or stealth or needs_browser) else None,
        proxy=ProxyPolicy.auto() if proxy else None,
        antibot=AntiBotPolicy.auto() if stealth else None,
    )


