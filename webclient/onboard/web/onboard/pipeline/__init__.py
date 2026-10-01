"""The staged onboarding pipeline -- see ``webclient/onboard/PIPELINE.md``."""

from __future__ import annotations

from .ask import Context, ReplyError, ask, ask_json
from .brief import Brief, BriefError, FieldSpec, SearchSpec, packaged_briefs
from .runner import STAGES, Stage, run
from .state import (
    STAGE_NAMES,
    ApiDescription,
    AuthorReview,
    CandidateReview,
    CrawlResult,
    DatasetSource,
    ExtractQuery,
    Hit,
    LocationReview,
    Onboarding,
    PaginateDescription,
    Pick,
    ResolvePlan,
    SearchResult,
    SearchReview,
    SpaDescription,
    Spend,
    StageLog,
    Visited,
)

__all__ = [
    "ApiDescription",
    "AuthorReview",
    "Brief",
    "BriefError",
    "CandidateReview",
    "Context",
    "CrawlResult",
    "DatasetSource",
    "ExtractQuery",
    "FieldSpec",
    "Hit",
    "LocationReview",
    "Onboarding",
    "PaginateDescription",
    "Pick",
    "ReplyError",
    "ResolvePlan",
    "STAGES",
    "STAGE_NAMES",
    "SearchResult",
    "SearchReview",
    "SearchSpec",
    "SpaDescription",
    "Spend",
    "Stage",
    "StageLog",
    "Visited",
    "ask",
    "ask_json",
    "packaged_briefs",
    "run",
]
