"""web.onboard -- the capstone: ``goal -> dataset``, and the LLM tier.

Composes the whole stack. Given a goal and seed URL(s): crawl for candidate pages (web.crawl over
a web.resolve Resolver), author a row extraction on the first page that yields data (web.onboard.agent's
loop driven by an :class:`Llm`), then apply that one Selection across every crawled page and
aggregate the rows -- each tagged with its source. One page's shape, reused; the model authors once.

The LLM itself lives here (not a separate layer): the :class:`Llm` protocol + :class:`AnthropicLlm`
client (:mod:`.llm`) and :func:`llm_driver` (:mod:`.llm_driver`, which bridges an ``Llm`` to
web.onboard.agent's pluggable ``Driver``). web.onboard.agent stays LLM-agnostic -- it takes any ``Driver`` callable.

    from web.onboard import onboard, AnthropicLlm
    result = await onboard("board members and their roles", "https://acme.com/board",
                           resolver=Resolver(), llm=AnthropicLlm())
    result.rows        # [{"name": ..., "role": ..., "_source": ...}, ...]
    result.selection   # the winning Selection (maps to a DSL plan for repeatable runs)
"""

from __future__ import annotations

from pydantic import BaseModel
from web.resolve import Resolver

from web.crawl import Crawler, Goal

from .agent import Author, Selection, extract
from .author import Authored as AuthoredQuery
from .author import author, authored, build_query
from .behaviours import Behaviour, apply_behaviours, behaviour, register_behaviour
from .compile import Query, QueryError, parse_query, reroot
from .llm import AnthropicLlm, Llm, Pricing, RateLimit, Usage
from .llm_driver import llm_driver
from .locate import Search, data_api_endpoints, locate
from .models import Brief, DatasetBrief, LocateBrief, Reference
from .patterns import PATTERNS_GUIDE, author_prompt
from .search import DdgSearch


class Onboarded(BaseModel):
    """The dataset: the aggregated rows (each with a ``_source`` url), the Selection that produced
    them, and how many pages were crawled."""

    rows: list[dict[str, "str | None"]] = []
    selection: "Selection | None" = None
    pages: int = 0


async def onboard(
    goal: str,
    seeds: "str | list[str]",
    *,
    resolver: Resolver,
    llm: Llm,
    max_pages: int = 40,
) -> Onboarded:
    """Crawl the seeds, author a row extraction (agent + llm) on the first page that yields data,
    then apply it across every crawled page and aggregate the rows."""
    docs = [doc async for doc in Crawler(resolver).crawl(Goal(start=seeds, max_pages=max_pages))]

    selection: "Selection | None" = None
    for doc in docs:  # author once, on the first page that produces rows
        authored = await Author(doc, llm_driver(llm, goal)).run()
        if authored.rows and authored.selection is not None:
            selection = authored.selection
            break
    if selection is None:
        return Onboarded(pages=len(docs))

    rows: list[dict[str, "str | None"]] = []
    for doc in docs:  # apply the one Selection everywhere; non-matching pages contribute nothing
        for row in extract(doc, selection):
            rows.append({**row, "_source": doc.url})
    return Onboarded(rows=rows, selection=selection, pages=len(docs))


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
    "onboard",
    "Onboarded",
    "Llm",
    "AnthropicLlm",
    "Usage",
    "Pricing",
    "RateLimit",
    "llm_driver",
    # the reusable Locate + Author phases and their value models
    "locate",
    "author",
    "authored",
    "build_query",
    "locate_and_author",
    "Reference",
    "Brief",
    "LocateBrief",
    "DatasetBrief",
    "AuthoredQuery",
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
]
