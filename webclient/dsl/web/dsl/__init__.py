"""web.dsl -- the lazy query DSL: the SAME surface as the monolith (``wq`` + typed
``Collection`` / ``Field``), lean-reimplemented on the packages cores.

``wq`` is the recorder: ``wq.reference(url)`` / ``wq.ref`` / ``wq.doc`` are lazy roots, each op
appends a step to a serialisable :class:`~web.dsl.plan.Plan`, and nothing runs until a terminal.
One recording, four dispatch modes:

    rows = await (wq.reference(url).resolve().select_all(".card")
                  .extract(title=wq.doc.select(".title").attr("text"),
                           price=wq.doc.select(".price").attr("text").number())
                  .filter(wq.doc.field("price") != "")
                  .acollect())                      # async -> list[dict] (smart: no explicit project)
    rows = (...).collect()                          # sync
    blob = (...).to_blob()                          # service / API
    rows = await run_blob(blob)                     # remote (server side)

Results are typed (:class:`~web.dsl.values.Field` / :class:`~web.dsl.values.Collection`), never
``object`` / ``Any``. :class:`WebClient` is the context-managed DSL entry (``async with WebClient()
as wc: await wc.resolve(url).doc()``) that owns the resolver lifecycle.
"""

from __future__ import annotations

from .expr import Expr, from_blob, from_plan
from .facade import WebClient
from .identity import IDENTITY_COLUMN, URL_COLUMN, digest, identity_of
from .plan import Arg, Plan, Step
from .query import Query, Run, RunEvent
from .report import ISSUES_COLUMN, Fetch, Issue, Report, assess
from .run import resolve_memo, resolve_memoised, run_blob
from .schema import DOCUMENT_TYPES, FieldDef, Schema, in_range, parse_range
from .sink import Attachment, Dataset, MemorySink, Sink, content_type_of, identity_key
from .surface import (
    KNOWN_VERBS,
    LazyCollection,
    LazyDocument,
    LazyField,
    LazyReference,
    LazyThen,
    SourceError,
    UnknownVerb,
    from_source,
    unknown_verbs,
    verbs_of,
    wq,
)
from .values import Collection, Field, Ref

__all__ = [
    "wq",
    "WebClient",
    "Expr",
    "from_blob",
    "from_plan",
    "from_source",
    "SourceError",
    "UnknownVerb",
    "KNOWN_VERBS",
    "verbs_of",
    "unknown_verbs",
    "run_blob",
    "resolve_memo",
    "resolve_memoised",
    "IDENTITY_COLUMN",
    "URL_COLUMN",
    "digest",
    "identity_of",
    "Plan",
    "Query",
    "Run",
    "RunEvent",
    "Report",
    "Fetch",
    "Issue",
    "ISSUES_COLUMN",
    "assess",
    "Schema",
    "FieldDef",
    "DOCUMENT_TYPES",
    "parse_range",
    "in_range",
    "Sink",
    "Dataset",
    "MemorySink",
    "Attachment",
    "identity_key",
    "content_type_of",
    "Step",
    "Arg",
    "Collection",
    "Field",
    "Ref",
    "LazyReference",
    "LazyDocument",
    "LazyCollection",
    "LazyField",
    "LazyThen",
]
