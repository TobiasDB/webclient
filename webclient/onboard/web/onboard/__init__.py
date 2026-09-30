"""web.onboard -- the capstone: ``goal -> dataset``, and the LLM tier.

The PROGRAMMATIC interface mirrors ``fetch()`` / ``resolve()``: :func:`locate` finds the entity's
source for a brief, :func:`author` writes the extraction query, and :func:`run` executes it and
routes the results to a :class:`Dataset` (rows + documents) or your own sink -- every dependency
defaulted from the env config (:mod:`.config`), so it is a one-liner:

    from web.onboard import author

    authored = await author("ir-events", "Acme United")   # locate + author (env-configured model)
    data = await authored.run()                            # Dataset(rows=[...], documents=[...])

The lower-level building blocks are still here: the core :func:`web.onboard.locate.locate` and the
one-shot :func:`web.onboard.author.author` (explicit resolver / LLM), :func:`build_query`,
:func:`author_agent`, and the :class:`Llm` clients (:class:`AnthropicLlm`, keyless :class:`ClaudeShim`).
"""

from __future__ import annotations

from web.resolve import Resolver

from .author import Authored as AuthoredQuery
from .author import AuthorEvent
from .author import author as author_query  # the reference-based one-shot primitive
from .author import authored, build_query
from .author_loop import author_agent
from .behaviours import Behaviour, apply_behaviours, behaviour, register_behaviour
from .compile import Query, QueryError, parse_query, reroot
from .config import build_resolver, default_llm, default_search
from .entries import Attachment, Authored, Dataset, author, locate, run
from .frontier import llm_frontier
from .llm import AnthropicLlm, Llm, LlmEvent, Pricing, RateLimit, ReasonEvent, Usage
from .locate import Search, data_api_endpoints
from .locate import locate as locate_source  # the core locate (explicit resolver/search/review)
from .models import Brief, DatasetBrief, LocateBrief, Reference, packaged_briefs
from .patterns import PATTERNS_GUIDE, author_prompt
from .review import review
from .search import DdgSearch
from .shim import ClaudeShim
from .sink import DOCUMENT_TYPES, MemorySink, Sink, document_fields, run_to_sink


async def locate_and_author(
    goal: "str | LocateBrief",
    brief: "DatasetBrief | None" = None,
    *,
    resolver: Resolver,
    llm: Llm,
    search: "Search | None" = None,
) -> "Query | None":
    """The thin composition over the PRIMITIVES: core locate then the one-shot author, sharing
    ``resolver``. Returns the ``wq`` query, or ``None`` if no source holds the dataset. (For the
    friendly env-defaulted flow, prefer :func:`author` which returns a runnable :class:`Authored`.)
    """
    reference = await locate_source(goal, resolver=resolver, search=search)
    if reference is None:
        return None
    return await author_query(reference, brief, resolver=resolver, llm=llm)


__all__ = [
    # -- the programmatic interface (env-defaulted, like fetch()/resolve()) --
    "locate",
    "author",
    "run",
    "Authored",
    "Dataset",
    "Attachment",
    # -- env config + default builders --
    "build_resolver",
    "default_llm",
    "default_search",
    # -- the LLM tier --
    "Llm",
    "AnthropicLlm",
    "ClaudeShim",
    "Usage",
    "Pricing",
    "RateLimit",
    "LlmEvent",
    "ReasonEvent",
    "llm_frontier",
    # -- lower-level building blocks --
    "locate_source",
    "author_query",
    "authored",
    "build_query",
    "author_agent",
    "locate_and_author",
    "Reference",
    "Brief",
    "LocateBrief",
    "DatasetBrief",
    "packaged_briefs",
    "AuthoredQuery",
    "AuthorEvent",
    "Search",
    "DdgSearch",
    "data_api_endpoints",
    "PATTERNS_GUIDE",
    "author_prompt",
    "Query",
    "QueryError",
    "parse_query",
    "reroot",
    "Behaviour",
    "behaviour",
    "register_behaviour",
    "apply_behaviours",
    "review",
    # -- sinks --
    "run_to_sink",
    "Sink",
    "MemorySink",
    "document_fields",
    "DOCUMENT_TYPES",
]
