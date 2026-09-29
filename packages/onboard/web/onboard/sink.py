"""The output pattern: run a ``wq`` query and route its results to TWO consumers at once -- scalar
ROWS to a table and DOCUMENTS (blobs) to an object store, each blob enriched with its row's
metadata. A field is a DOCUMENT when the brief's schema types it as one (``document`` / ``file`` /
``pdf`` / ``download`` / …); its value in the row is the file's URL, which the runner resolves to a
blob. A bare-URL result (a download listing -- ``select_all(...).attr('href')``) is all blobs.

A :class:`Sink` is the consumer contract (``record`` + ``blob``); plug a real DB table + object
store behind it. :class:`MemorySink` is the in-memory reference used in tests. So a DSL query runner
can, in one pass, populate a database table AND an object store keyed by the same records.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from pydantic import JsonValue
from web.resolve import Resolver

from .compile import Query
from .models import DatasetBrief

#: schema types whose VALUE is a downloadable document (the field's cell holds the file URL).
DOCUMENT_TYPES = frozenset({"document", "file", "blob", "pdf", "download", "attachment", "binary"})


@runtime_checkable
class Sink(Protocol):
    """A consumer of a query run: ``record`` a scalar row (a table row); ``blob`` a document's bytes
    with its ``metadata`` (the row's scalar fields) and a ``key`` (its URL). Both may fire per row --
    the record goes to the table, each document to the object store keyed by the same record."""

    async def record(self, row: "dict[str, JsonValue]") -> None: ...
    async def blob(
        self, content: bytes, *, key: str, content_type: str, metadata: "dict[str, JsonValue]"
    ) -> None: ...


@dataclass
class MemorySink:
    """An in-memory :class:`Sink` -- the reference consumer for tests / a dry run. ``rows`` is the
    table; ``blobs`` maps each document's URL to its ``(content, metadata)``."""

    rows: "list[dict[str, JsonValue]]" = field(default_factory=list)
    blobs: "dict[str, tuple[bytes, dict[str, JsonValue]]]" = field(default_factory=dict)

    async def record(self, row: "dict[str, JsonValue]") -> None:
        self.rows.append(row)

    async def blob(
        self, content: bytes, *, key: str, content_type: str, metadata: "dict[str, JsonValue]"
    ) -> None:
        self.blobs[key] = (content, metadata)


def document_fields(brief: DatasetBrief) -> "set[str]":
    """The brief's fields typed as a downloadable document (per :data:`DOCUMENT_TYPES`)."""
    return {f for f in brief.fields if brief.types.get(f, "").lower() in DOCUMENT_TYPES}


def _content_type(url: str) -> str:
    return mimetypes.guess_type(url.split("?")[0])[0] or "application/octet-stream"


async def run_to_sink(
    query: Query, brief: DatasetBrief, sink: Sink, *, resolver: Resolver
) -> "tuple[int, int]":
    """Run ``query`` and route its results to ``sink``: each scalar row -> ``record``; each
    document-typed field's URL (or a bare-URL row) -> resolved to a blob -> ``blob`` with the row's
    other fields as metadata. Returns ``(rows, blobs)`` counts. The document fetches reuse
    ``resolver`` (so a browser/profile is shared)."""
    doc_fields = document_fields(brief)
    result = await query.acollect(resolver=resolver)
    listed = result if isinstance(result, list) else [result]
    n_rows = n_blobs = 0
    for item in listed:
        if not isinstance(item, dict):  # a download listing (bare URLs) -- every result is a blob
            url = str(item)
            if url.startswith(("http://", "https://")):
                doc = await resolver.resolve(url)
                await sink.blob(doc.content, key=url, content_type=_content_type(url), metadata={})
                n_blobs += 1
            continue
        scalars: "dict[str, JsonValue]" = {k: v for k, v in item.items() if k not in doc_fields}
        await sink.record(scalars)
        n_rows += 1
        for (
            f
        ) in doc_fields:  # resolve each document field's URL to a blob, keep the row as metadata
            cell = item.get(f)
            if isinstance(cell, str) and cell.startswith(("http://", "https://")):
                doc = await resolver.resolve(cell)
                await sink.blob(
                    doc.content, key=cell, content_type=_content_type(cell), metadata=scalars
                )
                n_blobs += 1
    return n_rows, n_blobs


__all__ = ["Sink", "MemorySink", "run_to_sink", "document_fields", "DOCUMENT_TYPES"]
