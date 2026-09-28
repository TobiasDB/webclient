"""web.onboard -- the capstone: ``goal -> dataset``.

Composes the whole stack. Given a goal and seed URL(s): crawl for candidate pages (web.crawl over
a web.resolve Resolver), author a row extraction on the first page that yields data (web.agent
driven by a web.llm model), then apply that one Selection across every crawled page and aggregate
the rows -- each tagged with its source. One page's shape, reused; the model authors once.

    from web.onboard import onboard
    result = await onboard("board members and their roles", "https://acme.com/board",
                           resolver=Resolver(), llm=AnthropicLlm())
    result.rows        # [{"name": ..., "role": ..., "_source": ...}, ...]
    result.selection   # the winning Selection (maps to a DSL plan for repeatable runs)
"""

from __future__ import annotations

from pydantic import BaseModel

from web.agent import Author, Selection, extract, llm_driver
from web.crawl import Crawler, Goal
from web.llm import Llm
from web.resolve import Resolver


class Onboarded(BaseModel):
    """The dataset: the aggregated rows (each with a ``_source`` url), the Selection that produced
    them, and how many pages were crawled."""

    rows: list[dict[str, "str | None"]] = []
    selection: "Selection | None" = None
    pages: int = 0


async def onboard(
    goal: str, seeds: "str | list[str]", *, resolver: Resolver, llm: Llm, max_pages: int = 20,
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


__all__ = ["onboard", "Onboarded"]
