"""The programmatic interface -- ``onboard()`` and ``run()``, the onboard analogues of
``fetch()`` / ``resolve()``: give a brief (a :class:`Brief`, a packaged name, a file, or a bare
goal) and its arguments, get back the :class:`Onboarding` state (resumable; every stage's contract
on it), with EVERY dependency (resolver / model / web search) defaulted from the env config
(:mod:`web.onboard.config`). Then EXECUTE the authored query (:meth:`web.dsl.Query.run`): rows,
documents and a report, streamed to your own :class:`~web.dsl.Sink` if you pass one.

    from web.onboard import onboard, run
    state = await onboard("ir-news", company="Intel")      # the nine stages, env-configured
    result = await run(state)                               # Run(rows, documents, report)
"""

from __future__ import annotations

from pathlib import Path

from web.dsl import Query, Run, Sink
from web.resolve import Resolver

from .config import build_resolver, default_llm, default_search
from .llm import Llm
from .pipeline import Brief, Context, Onboarding
from .pipeline import run as _run_stages
from .search import Search


async def onboard(
    brief: "str | Brief",
    *,
    resolver: "Resolver | None" = None,
    llm: "Llm | None" = None,
    search: "Search | None" = None,
    state: "str | Path | None" = None,
    until: "str | None" = None,
    **values: str,
) -> Onboarding:
    """Onboard a dataset: run the staged pipeline for ``brief`` rendered with ``values`` (its
    declared arguments), resuming from ``state`` (a JSON file) when it exists and saving to it
    after every stage. Returns the :class:`Onboarding` -- ``state.author_extract`` holds the
    authored query, ``state.stopped`` says why a run ended early."""
    spec = brief if isinstance(brief, Brief) else Brief.load(brief)
    path = Path(state) if state is not None else None
    if path is not None and path.is_file():
        current = Onboarding.load(path)
    else:
        current = Onboarding.start(spec, **values)
    own = resolver is None
    resolver = resolver or build_resolver()
    ctx = Context(resolver=resolver, llm=llm or default_llm(), search=search or default_search())
    try:
        return await _run_stages(current, ctx, until=until, save=path)
    finally:
        if own:
            await resolver.aclose()


def query_of(state: Onboarding) -> "Query | None":
    """The authored :class:`~web.dsl.Query` of an onboarding (its schema from the brief), or
    ``None`` before / without a complete extraction."""
    ex = state.author_extract
    if ex is None or not ex.blob:
        return None
    return Query.from_blob(ex.blob)


async def run(
    source: "Onboarding | Query",
    *,
    resolver: "Resolver | None" = None,
    sink: "Sink | None" = None,
    lenient: bool = False,
) -> Run:
    """Execute an onboarding's authored query (or any :class:`~web.dsl.Query`) -- see
    :meth:`web.dsl.Query.run`: rows, documents (the schema's document-typed fields fetched) and the
    report, streamed to ``sink`` as they come. A resolver built here is closed on the way out."""
    query = query_of(source) if isinstance(source, Onboarding) else source
    if query is None:
        return Run()
    own = resolver is None
    resolver = resolver or build_resolver()
    try:
        return await query.run(resolver, sink=sink, lenient=lenient)
    finally:
        if own:
            await resolver.aclose()


__all__ = ["onboard", "query_of", "run"]
