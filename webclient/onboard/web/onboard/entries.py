"""The programmatic interface -- ``locate()`` and ``author()``, the onboard analogues of
``fetch()`` / ``resolve()``: give a brief (a :class:`Brief`, a packaged name, a file, or a bare
goal) plus an optional entity, and get back the located source or the authored query, with EVERY
dependency (resolver / LLM / web search) defaulted from the standardised env config (see
:mod:`web.onboard.config`). Then EXECUTE the authored query and route its results with a clean
:class:`Dataset` (scalar rows + fetched documents) or your own :class:`~web.onboard.sink.Sink`.

    from web.onboard import author

    authored = await author("ir-events", "Acme United")   # locate + author (env-configured model)
    data = await authored.run()                            # Dataset(rows=[...], documents=[...])
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import JsonValue
from web.dsl import KEY_COLUMN
from web.resolve import Resolver

from .author_loop import Engine, author_agent
from .compile import Query
from .config import build_resolver, default_llm, default_search, env
from .frontier import llm_frontier
from .llm import Llm
from .locate import Search
from .locate import locate as _locate_core
from .models import Brief, DatasetBrief, Reference
from .sink import Sink, run_to_sink


@dataclass
class Attachment:
    """A fetched document-typed field's content -- a file the dataset points at (its URL), resolved
    to bytes and kept with its row's scalar fields as ``metadata``."""

    url: str
    content: bytes
    content_type: str
    metadata: "dict[str, JsonValue]" = field(default_factory=dict)
    #: the document's IDENTITY: its row's key fields + ``url`` + ``hash`` (see the sink module),
    #: and ``key`` -- the one string a sync dedupes on.
    keys: "dict[str, JsonValue]" = field(default_factory=dict)
    key: str = ""


@dataclass
class Dataset:
    """An executed query's result, split by kind: scalar ROWS (the table) and fetched DOCUMENTS (the
    files the schema's document-typed fields point at, each keyed to its row)."""

    rows: "list[dict[str, JsonValue]]" = field(default_factory=list)  # each carries its `_key`
    documents: "list[Attachment]" = field(default_factory=list)
    schema: "dict[str, str]" = field(default_factory=dict)  # field -> type (+ `_key`)


class _Collector:
    """A :class:`~web.onboard.sink.Sink` that gathers into a :class:`Dataset` -- the default when no
    custom sink is given."""

    def __init__(self) -> None:
        self.data = Dataset()

    async def record(
        self, row: "dict[str, JsonValue]", *, key: str, schema: "dict[str, str]"
    ) -> None:
        self.data.schema = dict(schema)
        self.data.rows.append({**row, KEY_COLUMN: key})

    async def blob(
        self,
        content: bytes,
        *,
        key: str,
        keys: "dict[str, JsonValue]",
        content_type: str,
        metadata: "dict[str, JsonValue]",
    ) -> None:
        self.data.documents.append(
            Attachment(
                url=str(keys.get("url") or ""),
                content=content,
                content_type=content_type,
                metadata=metadata,
                keys=keys,
                key=key,
            )
        )


@dataclass
class Authored:
    """What :func:`author` returns: the located source and the authored SECTION queries (usually
    one; more for a split-source dataset). :meth:`run` executes every section, concatenates, and
    routes to a :class:`Dataset` or a custom sink."""

    reference: "Reference | None"
    queries: "list[Query]"
    brief: DatasetBrief

    def blobs(self) -> "list[str]":
        """Each section query's serialised (wire) form."""
        return [q.to_blob() for q in self.queries]

    def describe(self) -> str:
        """Each section query as a readable ``wq`` chain."""
        return "\n".join(q.describe() for q in self.queries)

    async def run(
        self, *, resolver: "Resolver | None" = None, sink: "Sink | None" = None
    ) -> "Dataset | None":
        """Execute the section queries (see :func:`run`)."""
        return await run(self, resolver=resolver, sink=sink)


def _frontier(llm: Llm, brief: Brief, entity: str) -> "tuple[object, ...]":
    return (
        llm_frontier(
            llm,
            brief.goal,
            entity=entity,
            fields=brief.fields,
            look=brief.look,
            ignore=brief.ignore,
        ),
    )


async def locate(
    brief: "str | Brief",
    entity: str = "",
    *,
    resolver: "Resolver | None" = None,
    llm: "Llm | None" = None,
    search: "Search | None" = None,
    profile: "str | None" = None,
    proxy: "str | None" = None,
    browser_path: "str | None" = None,
) -> "Reference | None":
    """Find the entity's own source for a brief and return a :class:`Reference` (or ``None`` -- Locate
    is allowed to fail). Every dependency defaults from the env config: the resolver (WEB_PROFILE /
    WEB_PROXY / WEB_BROWSER_PATH), the LLM (WEB_LLM_*, used for the entity-aware crawl frontier + the
    candidate review), and the web search. A resolver built here is closed on the way out."""
    lb = Brief.resolve(brief).with_entity(entity)
    own = resolver is None
    resolver = resolver or build_resolver(profile=profile, proxy=proxy, browser_path=browser_path)
    llm = llm or default_llm()
    try:
        return await _locate_core(
            lb,
            resolver=resolver,
            search=search or default_search(),
            frontier=_frontier(llm, lb, entity),  # type: ignore[arg-type]
            entity=entity,
            review=llm,
        )
    finally:
        if own:
            await resolver.aclose()


async def author(
    source: "str | Brief | Reference",
    brief: "str | Brief | None" = None,
    entity: str = "",
    *,
    resolver: "Resolver | None" = None,
    llm: "Llm | None" = None,
    profile: "str | None" = None,
    proxy: "str | None" = None,
    browser_path: "str | None" = None,
    engine: "Engine | None" = None,
) -> Authored:
    """Author the extraction query for a dataset. ``source`` is a :class:`Reference` (author it) OR a
    brief spec (locate it first, then author) -- with an optional ``brief`` to describe a Reference's
    schema. Env-defaulted like :func:`locate`; ``engine`` (``WEB_AUTHOR_ENGINE``) picks how the
    loop writes the query. Returns an :class:`Authored` (the section queries); call
    :meth:`Authored.run` to execute + sink."""
    if isinstance(source, Reference):
        reference: "Reference | None" = source
        lb = (Brief.resolve(brief) if brief is not None else DatasetBrief()).with_entity(entity)
    else:
        lb = Brief.resolve(source).with_entity(entity)
        reference = None
    own = resolver is None
    resolver = resolver or build_resolver(profile=profile, proxy=proxy, browser_path=browser_path)
    llm = llm or default_llm()
    try:
        if reference is None:
            reference = await _locate_core(
                lb,
                resolver=resolver,
                search=default_search(),
                frontier=_frontier(llm, lb, entity),  # type: ignore[arg-type]
                entity=entity,
                review=llm,
            )
        if reference is None:
            return Authored(reference=None, queries=[], brief=lb)
        picked = engine or env("WEB_AUTHOR_ENGINE") or "chain"
        queries, _verdict = await author_agent(
            reference,
            lb,
            resolver=resolver,
            llm=llm,
            review=llm,
            entity=entity,
            engine="steps" if picked == "steps" else "chain",
        )
        return Authored(reference=reference, queries=queries, brief=lb)
    finally:
        if own:
            await resolver.aclose()


async def run(
    source: "Authored | Query | list[Query]",
    *,
    resolver: "Resolver | None" = None,
    sink: "Sink | None" = None,
    brief: "DatasetBrief | None" = None,
) -> "Dataset | None":
    """Execute an authored query and route its results: scalar rows -> the table, document-typed
    fields' URLs -> fetched blobs (keyed to their row). ``source`` is an :class:`Authored`, a single
    query, or a list of section queries; the ``brief`` (an Authored carries its own -- else pass one)
    types which fields are documents. Without a ``sink``, returns an in-memory :class:`Dataset`
    (``rows`` + ``documents``); with a custom :class:`~web.onboard.sink.Sink`, streams to it and
    returns ``None``. A resolver built here is closed on the way out."""
    if isinstance(source, Authored):
        queries, spec = source.queries, brief or source.brief
    elif isinstance(source, list):
        queries, spec = source, brief or DatasetBrief()
    else:
        queries, spec = [source], brief or DatasetBrief()
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


__all__ = ["locate", "author", "run", "Authored", "Dataset", "Attachment"]
