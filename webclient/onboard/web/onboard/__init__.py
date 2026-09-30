"""web.onboard -- the capstone: ``goal -> dataset``, and the LLM tier.

Composes the whole stack as two phases. LOCATE (:func:`locate`) finds the best SOURCE for a goal --
crawl/search for candidate pages over a web.resolve Resolver, score them, and (optionally) let an
LLM review the winner. AUTHOR (:func:`author`) then writes a ``wq`` extraction query for that source
over the patterns guide + the page's signals/flags. :func:`locate_and_author` is the thin
composition of the two.

The LLM itself lives here (not a separate layer): the :class:`Llm` protocol + :class:`AnthropicLlm`
client (:mod:`.llm`), with :class:`ClaudeShim` a keyless local ``claude -p`` stand-in.

    from web.onboard import locate_and_author, AnthropicLlm, DdgSearch
    query = await locate_and_author("board members and their roles",
                                    resolver=Resolver(), llm=AnthropicLlm(), search=DdgSearch())
    rows = await query.acollect(resolver=Resolver())  # the wq query runs (or ships as a blob)
"""

from __future__ import annotations

from web.resolve import Resolver

from .author import Authored as AuthoredQuery
from .author import AuthorEvent, author, authored, build_query
from .author_loop import author_agent
from .behaviours import Behaviour, apply_behaviours, behaviour, register_behaviour
from .compile import Query, QueryError, parse_query, reroot
from .frontier import llm_frontier
from .llm import AnthropicLlm, Llm, LlmEvent, Pricing, RateLimit, ReasonEvent, Usage
from .locate import Search, data_api_endpoints, locate
from .models import Brief, DatasetBrief, LocateBrief, Reference
from .patterns import PATTERNS_GUIDE, author_prompt
from .review import review
from .search import DdgSearch
from .shim import ClaudeShim
from .sink import MemorySink, Sink, document_fields, run_to_sink


async def locate_and_author(
    goal: "str | LocateBrief",
    brief: "DatasetBrief | None" = None,
    *,
    resolver: Resolver,
    llm: Llm,
    search: "Search | None" = None,
) -> "Query | None":
    """The thin composition ``author ∘ locate``: find the best source for ``goal`` (:func:`locate`,
    deterministic) and, if one is found, write the extraction query for it (:func:`author`, driven
    by ``llm`` over the patterns guide). Returns the ``wq`` query (run or ship it), or ``None`` if no
    source holds the dataset. Both phases share ``resolver``."""
    reference = await locate(goal, resolver=resolver, search=search)
    if reference is None:
        return None
    return await author(reference, brief, resolver=resolver, llm=llm)


__all__ = [
    "Llm",
    "AnthropicLlm",
    "ClaudeShim",
    "Usage",
    "Pricing",
    "RateLimit",
    "LlmEvent",
    "ReasonEvent",
    "llm_frontier",
    # the reusable Locate + Author phases and their value models
    "locate",
    "author",
    "authored",
    "build_query",
    "author_agent",
    "locate_and_author",
    "Reference",
    "Brief",
    "LocateBrief",
    "DatasetBrief",
    "AuthoredQuery",
    "AuthorEvent",
    "Search",
    "DdgSearch",
    "data_api_endpoints",
    # the natural-language patterns knowledge + the safe query compiler
    "PATTERNS_GUIDE",
    "author_prompt",
    "Query",
    "QueryError",
    "parse_query",
    "reroot",
    # the flag/signal-keyed behaviours (advisory notes over the authored query)
    "Behaviour",
    "behaviour",
    "register_behaviour",
    "apply_behaviours",
    "review",
    "run_to_sink",
    "Sink",
    "MemorySink",
    "document_fields",
]
