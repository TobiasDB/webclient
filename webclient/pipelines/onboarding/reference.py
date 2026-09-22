"""onboarding.reference -- see the package docstring."""


import abc
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Sequence

from pydantic import BaseModel, model_validator

#: the pipeline's logger. Stages log progress here (seeds, crawl, candidates, the
#: evaluation, the query, spend); a CLI or app sets the level / handler. Each line is
#: also appended to ``OnboardingResult.steps`` for a programmatic trace.

from ...core.crawl import from_picks
from ...core.document.models import Flag
from ...policy import (
    AntiBotPolicy,
    BrowserPolicy,
    ProxyPolicy,
    Resolve,
)
from ...llm.guides import lazy_query_guide
from ...query.expr import from_blob
from ...interface import Reference, WebClient, wq
from ...clients.llm import Budget, BudgetExceeded, LlmClient, LlmError
from ...llm.prompts import render_prompt

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


def write_resolve(flags: Sequence[Flag]) -> Resolve:
    """The ``Resolve`` policy for the source -- deterministic from its flags. A
    ``spa`` needs a browser render; an ``anti_bot_triggered`` needs its remedy
    (``proxy`` for a bare block, ``stealth`` = a browser behind a proxy with anti-bot
    handling for a named vendor). A login wall has no transport remedy."""
    by = {f.name: f for f in flags if f.present}
    # a browser is needed to build the DOM: an SPA composes it client-side; shadow DOM /
    # a same-origin iframe hides content a plain HTML snapshot misses, and only a render
    # inlines it (see the __wc_inline page script).
    needs_render = any(n in by for n in ("spa", "shadow_dom", "iframe"))
    triggered = by.get("anti_bot_triggered")
    stealth = bool(triggered and triggered.remedy == "stealth")
    proxy = bool(triggered and triggered.remedy in ("proxy", "stealth"))
    return Resolve(
        browser=BrowserPolicy(when="always") if (needs_render or stealth) else None,
        proxy=ProxyPolicy.auto() if proxy else None,
        antibot=AntiBotPolicy.auto() if stealth else None,
    )


