"""web.onboard -- the capstone: ``brief -> dataset``, and the model tier.

The PROGRAMMATIC interface mirrors ``fetch()`` / ``resolve()``: :func:`onboard` runs the staged
pipeline (search -> review -> crawl -> review -> expand -> review -> resolve -> extract -> review;
see ``webclient/onboard/PIPELINE.md``) for a brief and its arguments and returns the resumable
:class:`Onboarding` state; :func:`run` executes the authored query (``web.dsl.Query.run``) into a
:class:`Run` (rows + documents + report), streamed to your own sink if you pass one -- every dependency defaulted from the env
config (:mod:`.config`):

    from web.onboard import onboard, run
    state = await onboard("ir-news", company="Intel")
    result = await run(state)                               # Run(rows, documents, report)

The building blocks are here too: the stage contracts + :func:`web.onboard.pipeline.run` with an
explicit :class:`Context`, the ``wq`` compile step (:func:`parse_query` / :func:`reroot`), and the
:class:`Llm` clients (:class:`AnthropicLlm`, the keyless :class:`ClaudeShim`).
"""

from __future__ import annotations

from web.dsl import Attachment, Dataset, MemorySink, Query, Run, Sink, identity_key

from .compile import QueryError, hygienic, parse_query, reroot
from .config import build_resolver, default_llm, default_search
from .entries import onboard, query_of, run
from .llm import (
    AnthropicLlm,
    Budget,
    BudgetExceeded,
    Conversation,
    Conversational,
    Llm,
    LlmEvent,
    Pricing,
    RateLimit,
    ReasonEvent,
    Usage,
)
from .models import SearchHit
from .pipeline import (
    STAGE_NAMES,
    STAGES,
    ApiDescription,
    AuthorReview,
    Brief,
    BriefError,
    CandidateReview,
    Context,
    CrawlResult,
    DatasetSource,
    ExtractQuery,
    FieldSpec,
    Hit,
    LocationReview,
    Onboarding,
    PaginateDescription,
    Pick,
    ReplyError,
    ResolvePlan,
    SearchResult,
    SearchReview,
    SearchSpec,
    SpaDescription,
    Spend,
    Stage,
    StageLog,
    Visited,
    packaged_briefs,
)
from .search import DdgSearch, Search
from .shim import ClaudeShim

__all__ = [
    # -- the programmatic interface --
    "onboard",
    "run",
    "query_of",
    "Dataset",
    "Attachment",
    "Run",
    "Onboarding",
    "Brief",
    "BriefError",
    "FieldSpec",
    "SearchSpec",
    "packaged_briefs",
    # -- the stages and their contracts --
    "Context",
    "Stage",
    "STAGES",
    "STAGE_NAMES",
    "StageLog",
    "Spend",
    "Hit",
    "SearchResult",
    "Pick",
    "SearchReview",
    "Visited",
    "CrawlResult",
    "CandidateReview",
    "DatasetSource",
    "PaginateDescription",
    "ApiDescription",
    "SpaDescription",
    "LocationReview",
    "ResolvePlan",
    "ExtractQuery",
    "AuthorReview",
    "ReplyError",
    # -- compile --
    "Query",
    "QueryError",
    "parse_query",
    "reroot",
    "hygienic",
    # -- the model tier --
    "Llm",
    "Conversation",
    "Conversational",
    "AnthropicLlm",
    "ClaudeShim",
    "Pricing",
    "RateLimit",
    "Budget",
    "BudgetExceeded",
    "Usage",
    "LlmEvent",
    "ReasonEvent",
    # -- search / config / sinks --
    "Search",
    "DdgSearch",
    "SearchHit",
    "build_resolver",
    "default_llm",
    "default_search",
    "Sink",
    "MemorySink",
    "identity_key",
    "Run",
]
