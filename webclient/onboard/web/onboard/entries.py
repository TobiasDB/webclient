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

from collections.abc import Sequence
from pathlib import Path

from web.dsl import Query, Report, Run, Sink
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
    related: "Sequence[Onboarding] | None" = None,
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
        others = list(related) if related is not None else related_onboardings(spec, values, path)
        current = Onboarding.start(spec, others, **values)
    own = resolver is None
    resolver = resolver or build_resolver()
    ctx = Context(resolver=resolver, llm=llm or default_llm(), search=search or default_search())
    try:
        return await _run_stages(current, ctx, until=until, save=path)
    finally:
        if own:
            await resolver.aclose()


def state_path(brief: Brief, values: "dict[str, str]", directory: "Path | None" = None) -> Path:
    """Where an onboarding of ``brief`` with ``values`` is saved by convention:
    ``<brief>-<arg values>.json`` in ``directory`` (the working directory by default)."""
    tail = "-".join(v.lower().replace(" ", "_") for v in values.values())
    name = f"{brief.name or 'brief'}{'-' + tail if tail else ''}.json"
    return (directory or Path(".")) / name


def related_onboardings(
    brief: Brief, values: "dict[str, str]", state: "Path | None" = None
) -> "list[Onboarding]":
    """The finished onboardings of ``brief``'s related briefs for the same ``values``, found by
    the state-file convention next to this onboarding's state file."""
    directory = state.parent if state is not None else Path(".")
    out: list[Onboarding] = []
    for name in brief.related:
        try:
            other = Brief.load(name)
        except Exception:  # noqa: BLE001 -- an unknown related brief is skipped
            continue
        path = state_path(other, {k: v for k, v in values.items() if k in other.args}, directory)
        if path.is_file():
            out.append(Onboarding.load(path))
    return out


def queries_of(state: Onboarding) -> "list[Query]":
    """The authored :class:`~web.dsl.Query` per authoring guide (schema from the brief) -- empty
    before / without a complete extraction."""
    return [Query.from_blob(ex.blob) for ex in (state.author_extract or []) if ex.blob]


def query_of(state: Onboarding) -> "Query | None":
    """The FIRST authored query (a single-guide brief has exactly one), or ``None``."""
    found = queries_of(state)
    return found[0] if found else None


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
    queries = queries_of(source) if isinstance(source, Onboarding) else [source]
    own = resolver is None
    resolver = resolver or build_resolver()
    total = Run()
    try:
        for query in queries:
            part = await query.run(resolver, sink=sink, lenient=lenient)
            total.rows.extend(part.rows)
            total.documents.extend(part.documents)
            total.report = (
                _merge(total.report, part.report) if total.rows != part.rows else part.report
            )
    finally:
        if own:
            await resolver.aclose()
    return total


def _merge(a: Report, b: Report) -> Report:
    """Two queries' reports as one: counts summed, fill weighted by rows, the rest concatenated."""
    rows = a.rows + b.rows
    fill = {
        n: ((a.fill.get(n, 0.0) * a.rows) + (b.fill.get(n, 0.0) * b.rows)) / rows if rows else 0.0
        for n in {*a.fill, *b.fill}
    }
    return Report(
        rows=rows,
        expected=b.expected or a.expected,
        fill=fill,
        duplicates=a.duplicates + b.duplicates,
        newest=max(a.newest, b.newest),
        newest_age_days=(
            min(x for x in (a.newest_age_days, b.newest_age_days) if x is not None)
            if (a.newest_age_days is not None or b.newest_age_days is not None)
            else None
        ),
        issues=[*a.issues, *b.issues],
        fetches=[*a.fetches, *b.fetches],
        failures=[*a.failures, *b.failures],
        elapsed_s=round(a.elapsed_s + b.elapsed_s, 2),
    )


__all__ = ["onboard", "queries_of", "query_of", "related_onboardings", "run", "state_path"]
