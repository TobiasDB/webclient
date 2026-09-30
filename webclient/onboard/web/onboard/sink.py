"""The output pattern: run a ``wq`` query and route its results to TWO consumers at once -- scalar
ROWS to a table and DOCUMENTS (blobs) to an object store. Identity is the QUERY's business (the
author declares ``.identity(...)`` where a record or a document needs one -- see
:mod:`web.dsl.identity`); what to do with it -- dedupe, upsert, version -- is the SINK's.

A field is a DOCUMENT when the brief's schema types it as one (``document`` / ``file`` / ``pdf`` /
``download`` / …); its value in the row is the file's URL, which the runner resolves to a blob. A
page the query fanned out into and gave an identity (a per-record ``.resolve().extract(...)
.identity(...)``) is a document too: its bytes come from the run's resolve memo (no refetch) and
its metadata is the nested row (the fields read from it, its ``_identity``, its ``_url``) plus the
parent row. A bare-URL result (a download listing -- ``select_all(...).attr('href')``) is all blobs.

A :class:`Sink` is the consumer contract (``record`` + ``blob``); plug a real DB table + object
store behind it. :class:`MemorySink` is the in-memory reference -- append-only on ``_identity``
when a row / document carries one -- used in tests.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from pydantic import JsonValue
from web.dsl import IDENTITY_COLUMN, URL_COLUMN, digest, resolve_memo
from web.resolve import Resolver

from .compile import Query
from .models import DatasetBrief

#: schema types whose VALUE is a downloadable document (the field's cell holds the file URL).
DOCUMENT_TYPES = frozenset({"document", "file", "blob", "pdf", "download", "attachment", "binary"})


@runtime_checkable
class Sink(Protocol):
    """A consumer of a query run. ``record`` a row (as extracted -- nested rows, ``_identity`` /
    ``_url`` columns and all) with the row ``schema`` (field -> type); ``blob`` a document's bytes
    with its ``url``, ``content_type`` and ``metadata`` (the row it belongs to; for a fanned-out
    page also the fields read from it). Both may fire per row -- the record goes to the table,
    each document to the object store."""

    async def record(self, row: "dict[str, JsonValue]", *, schema: "dict[str, str]") -> None: ...
    async def blob(
        self, content: bytes, *, url: str, content_type: str, metadata: "dict[str, JsonValue]"
    ) -> None: ...


def identity_key(row: "dict[str, JsonValue]") -> str:
    """The string a sink dedupes a row on: its ``_identity`` when the query declared one, else a
    digest of the whole row."""
    ident = row.get(IDENTITY_COLUMN)
    return ident if isinstance(ident, str) and ident else digest([row])


@dataclass
class MemorySink:
    """An in-memory :class:`Sink` -- the reference consumer for tests / a dry run, APPEND-ONLY: a
    row whose identity (see :func:`identity_key`) was already stored is skipped, as is a blob
    whose url + identity was; ``skipped`` counts them. ``rows`` is the table; ``blobs`` maps a
    document key (``url#identity``) to ``(content, metadata)``; ``schema`` is the last row schema
    seen."""

    rows: "list[dict[str, JsonValue]]" = field(default_factory=list)
    blobs: "dict[str, tuple[bytes, dict[str, JsonValue]]]" = field(default_factory=dict)
    schema: "dict[str, str]" = field(default_factory=dict)
    seen: "set[str]" = field(default_factory=set)
    skipped: int = 0

    async def record(self, row: "dict[str, JsonValue]", *, schema: "dict[str, str]") -> None:
        self.schema = dict(schema)
        key = identity_key(row)
        if key in self.seen:
            self.skipped += 1
            return
        self.seen.add(key)
        self.rows.append(row)

    async def blob(
        self, content: bytes, *, url: str, content_type: str, metadata: "dict[str, JsonValue]"
    ) -> None:
        fields = metadata.get("fields")
        ident = fields.get(IDENTITY_COLUMN) if isinstance(fields, dict) else None
        key = f"{url}#{ident}" if isinstance(ident, str) else f"{url}#{digest([content.hex()])}"
        if key in self.blobs:
            self.skipped += 1
            return
        self.blobs[key] = (content, metadata)


def document_fields(brief: DatasetBrief) -> "set[str]":
    """The brief's fields typed as a downloadable document (per :data:`DOCUMENT_TYPES`)."""
    return {f for f in brief.fields if brief.types.get(f, "").lower() in DOCUMENT_TYPES}


def row_schema(brief: DatasetBrief) -> "dict[str, str]":
    """The row schema a sink receives: every brief field -> its type (``string`` when untyped),
    plus the identity column."""
    schema = {f: (brief.types.get(f) or "string") for f in brief.fields}
    schema[IDENTITY_COLUMN] = "identity"
    return schema


def _content_type(url: str) -> str:
    return mimetypes.guess_type(url.split("?")[0])[0] or "application/octet-stream"


def _pages(value: JsonValue) -> "list[dict[str, JsonValue]]":
    """Every nested row that came from a document and carries an identity (``_url`` set by the
    ``identity`` verb), at any depth."""
    found: list[dict[str, JsonValue]] = []
    if isinstance(value, dict):
        if isinstance(value.get(URL_COLUMN), str):
            found.append(value)
        for v in value.values():
            found.extend(_pages(v))
    elif isinstance(value, list):
        for v in value:
            found.extend(_pages(v))
    return found


async def run_to_sink(
    query: Query, brief: DatasetBrief, sink: Sink, *, resolver: Resolver
) -> "tuple[int, int]":
    """Run ``query`` and route its results to ``sink`` (see the module docstring): each row ->
    ``record`` with the schema; each document-typed field's URL, each identified page the query
    fanned out into, and each bare-URL row -> resolved to a blob -> ``blob``. Returns ``(rows,
    blobs)`` counts. Pages the query already fetched come from the run's resolve memo."""
    doc_fields = document_fields(brief)
    schema = row_schema(brief)
    n_rows = n_blobs = 0
    with resolve_memo():
        result = await query.acollect(resolver=resolver)
        listed = result if isinstance(result, list) else [result]
        for item in listed:
            if not isinstance(
                item, dict
            ):  # a download listing (bare URLs) -- every result is a blob
                url = str(item)
                if url.startswith(("http://", "https://")):
                    doc = await resolver.resolve(url)
                    await sink.blob(
                        doc.content, url=url, content_type=_content_type(url), metadata={}
                    )
                    n_blobs += 1
                continue
            await sink.record(item, schema=schema)
            n_rows += 1
            scalars: dict[str, JsonValue] = {
                k: v for k, v in item.items() if not isinstance(v, (dict, list))
            }
            for page in _pages(item):  # the identified pages the query fanned out into
                url = str(page[URL_COLUMN])
                doc = await resolver.resolve(url)
                await sink.blob(
                    doc.content,
                    url=url,
                    content_type=_content_type(url),
                    metadata={"row": scalars, "fields": page},
                )
                n_blobs += 1
            for f in doc_fields:  # each document-typed field's file, with its row as metadata
                cell = item.get(f)
                if isinstance(cell, str) and cell.startswith(("http://", "https://")):
                    doc = await resolver.resolve(cell)
                    await sink.blob(
                        doc.content,
                        url=cell,
                        content_type=_content_type(cell),
                        metadata={"row": scalars, "field": f},
                    )
                    n_blobs += 1
    return n_rows, n_blobs


__all__ = [
    "Sink",
    "MemorySink",
    "run_to_sink",
    "document_fields",
    "identity_key",
    "row_schema",
    "DOCUMENT_TYPES",
]
