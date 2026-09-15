"""Higher-level pipelines composed from the web-client surface.

These are *orchestrations* (search -> crawl -> evaluate -> author a query), not
part of the core library: they use only the public surface (``WebClient`` verbs,
``crawl``, ``skeleton``, ``signals``, the lazy-query DSL) plus an injected LLM, so
they stay testable offline with a stub model.
"""

from __future__ import annotations

from .onboarding import (
    Brief,
    Candidate,
    CandidateEval,
    OnboardingResult,
    SearchHit,
    Seed,
    crawl_from_seeds,
    ddg_search,
    evaluate_candidate,
    evaluate_candidates,
    onboard,
    onboard_company,
    search_web,
    select_candidates,
    write_query,
    write_reference,
    write_resolve,
)

__all__ = [
    "Brief",
    "Seed",
    "SearchHit",
    "Candidate",
    "CandidateEval",
    "OnboardingResult",
    "search_web",
    "ddg_search",
    "crawl_from_seeds",
    "select_candidates",
    "evaluate_candidate",
    "evaluate_candidates",
    "write_reference",
    "write_resolve",
    "write_query",
    "onboard_company",
    "onboard",
]
