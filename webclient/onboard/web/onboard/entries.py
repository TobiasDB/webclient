"""The programmatic interface -- ``onboard()`` and ``run()``, the onboard analogues of
``fetch()`` / ``resolve()``: give a brief (a :class:`Brief`, a packaged name, a file, or a bare
goal) and its arguments, get back the :class:`Onboarding` state (resumable; every stage's contract
on it), with EVERY dependency (resolver / model / web search) defaulted from the env config
(:mod:`web.onboard.config`). Then EXECUTE the authored query and route its results with a clean
:class:`Dataset` (scalar rows + fetched documents) or your own :class:`~web.onboard.sink.Sink`.

    from web.onboard import onboard, run
    state = await onboard("ir-news", company="Intel")      # the nine stages, env-configured
    data = await run(state)                                 # Dataset(rows=[...], documents=[...])
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from pydantic import JsonValue
from web.dsl import from_blob
from web.resolve import Resolver

from .compile import Query
from .config import build_resolver, default_llm, default_search
from .llm import Llm
from .pipeline import Brief, Context, Onboarding
from .pipeline import run as _run_stages
from .search import Search
from .sink import Sink, run_to_sink


@dataclass
class Attachment:
    """A fetched document-typed field's content -- a file the dataset points at (its URL), resolved
    to bytes and kept with its row's scalar fields as ``metadata``."""

    url: str
    content: bytes
    content_type: str
    metadata: "dict[str, JsonValue]" = field(default_factory=dict)


@dataclass
class Dataset:
    """An executed query's result, split by kind: scalar ROWS (the table) and fetched DOCUMENTS (the
    files the schema's document-typed fields point at, each keyed to its row)."""

    rows: "list[dict[str, JsonValue]]" = field(default_factory=list)
    documents: "list[Attachment]" = field(default_factory=list)
    schema: "dict[str, str]" = field(default_factory=dict)


class _Collector:
    """A :class:`~web.onboard.sink.Sink` that gathers into a :class:`Dataset`."""

    def __init__(self) -> None:
        self.data = Dataset()

    async def record(self, row: "dict[str, JsonValue]", *, schema: "dict[str, str]") -> None:
        self.data.schema = dict(schema)
        self.data.rows.append(row)

    async def blob(
        self, content: bytes, *, url: str, content_type: str, metadata: "dict[str, JsonValue]"
    ) -> None:
        self.data.documents.append(
            Attachment(url=url, content=content, content_type=content_type, metadata=metadata)
        )


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
    """The authored query of an onboarding (``None`` before / without a complete extraction)."""
    ex = state.author_extract
    if ex is None or not ex.blob:
        return None
    return cast(Query, from_blob(ex.blob))


async def run(
    source: "Onboarding | Query | list[Query]",
    *,
    resolver: "Resolver | None" = None,
    sink: "Sink | None" = None,
    brief: "Brief | None" = None,
) -> "Dataset | None":
    """Execute an authored query and route its results: scalar rows -> the table, document-typed
    fields' URLs -> fetched blobs (keyed to their row). ``source`` is an :class:`Onboarding`, a
    query, or a list of queries; the ``brief`` (an Onboarding carries its own) types which fields
    are documents. Without a ``sink``, returns an in-memory :class:`Dataset`; with a custom
    :class:`~web.onboard.sink.Sink`, streams to it and returns ``None``."""
    if isinstance(source, Onboarding):
        q = query_of(source)
        queries, spec = ([q] if q is not None else []), brief or source.brief
    elif isinstance(source, list):
        queries, spec = source, brief or Brief()
    else:
        queries, spec = [source], brief or Brief()
    own = resolver is None
    resolver = resolver or build_resolver()
    collector = _Collector() if sink is None else None
    target: Sink = sink if sink is not None else collector  # type: ignore[assignment]
    try:
        for q in queries:
            await run_to_sink(q, spec, target, resolver=resolver)
    finally:
        if own:
            await resolver.aclose()
    return collector.data if collector is not None else None


__all__ = ["Attachment", "Dataset", "onboard", "query_of", "run"]
