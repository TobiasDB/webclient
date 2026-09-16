"""Higher-level pipelines composed from the web-client surface.

These are *orchestrations* (search -> crawl -> evaluate -> author a query), not
part of the core library: they use only the public surface (``WebClient`` verbs,
``crawl``, ``skeleton``, ``signals``, the lazy-query DSL) plus an injected LLM, so
they stay testable offline with a stub model.
"""

from __future__ import annotations

from .llm import (
    Budget,
    BudgetExceeded,
    LlmClient,
    ModelPrice,
    Usage,
    price_for,
)
from .onboarding import (
    Brief,
    SchemaField,
    Candidate,
    CandidateEval,
    OnboardingResult,
    Review,
    SearchHit,
    Seed,
    crawl_from_seeds,
    ddg_search,
    evaluate_candidate,
    evaluate_candidates,
    diagnose_failure,
    onboard,
    onboard_company,
    search_web,
    select_candidates,
    run_query,
    write_query,
    write_reference,
    write_resolve,
)
from .prompts import render_prompt

__all__ = [
    "Brief",
    "SchemaField",
    "Seed",
    "SearchHit",
    "Candidate",
    "CandidateEval",
    "OnboardingResult",
    "Review",
    "diagnose_failure",
    "search_web",
    "ddg_search",
    "crawl_from_seeds",
    "select_candidates",
    "evaluate_candidate",
    "evaluate_candidates",
    "write_reference",
    "write_resolve",
    "run_query",
    "write_query",
    "onboard_company",
    "onboard",
    # LLM client + budget
    "LlmClient",
    "Budget",
    "BudgetExceeded",
    "Usage",
    "ModelPrice",
    "price_for",
    "render_prompt",
]
