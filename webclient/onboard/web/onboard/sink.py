"""The output pattern: run a ``wq`` query and route its results to TWO consumers at once -- scalar
ROWS to a table and DOCUMENTS (blobs) to an object store -- each carrying its IDENTITY, so a query
run daily syncs APPEND-ONLY (the snapshot model):

* a row's identity is its KEY FIELDS (the brief's ``key``, written by the query's ``key(...)``
  step as ``_key``; derived here when the query has none), delivered with the row's SCHEMA;
* a document's identity is the row's key fields plus a CONTENT HASH -- a document-typed field's
  file hashed as fetched; a page the query fanned out into (a resolved detail page) hashed over
  its STABLE part (the ``_doc`` identity the DSL puts on the row; ``key(..., document=css)`` picks
  the selector) -- delivered with the row's scalar fields as metadata.

A field is a DOCUMENT when the brief's schema types it as one (``document`` / ``file`` / ``pdf`` /
``download`` / …); its value in the row is the file's URL, which the runner resolves to a blob. A
bare-URL result (a download listing -- ``select_all(...).attr('href')``) is all blobs.

A :class:`Sink` is the consumer contract (``record`` + ``blob``); plug a real DB table + object
store behind it. :class:`MemorySink` is the in-memory reference -- append-only by key, as a real
sink would be -- used in tests. So a DSL query runner can, in one pass, populate a database table
AND an object store keyed by the same records.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from pydantic import JsonValue
from web.dsl import DOC_COLUMN, KEY_COLUMN, Plan, digest, key_fields, resolve_memo, row_key
from web.resolve import Resolver

from .compile import Query
from .models import DatasetBrief

#: schema types whose VALUE is a downloadable document (the field's cell holds the file URL).
DOCUMENT_TYPES = frozenset({"document", "file", "blob", "pdf", "download", "attachment", "binary"})


@runtime_checkable
class Sink(Protocol):
    """A consumer of a query run. ``record`` a scalar row (a table row) with its identity ``key``
    and the row ``schema`` (field -> type); ``blob`` a document's bytes with its ``key`` (the
    identity string), its ``keys`` (the identity FIELDS: the row's key fields, ``row`` -- the row's key --, ``url`` + ``hash``),
    ``content_type`` and ``metadata`` (the row's scalar fields). Both may fire per row -- the record
    goes to the table, each document to the object store keyed by the same record. An append-only
    sink skips a key it has already stored."""

    async def record(
        self, row: "dict[str, JsonValue]", *, key: str, schema: "dict[str, str]"
    ) -> None: ...
    async def blob(
        self,
        content: bytes,
        *,
        key: str,
        keys: "dict[str, JsonValue]",
        content_type: str,
        metadata: "dict[str, JsonValue]",
    ) -> None: ...


@dataclass
class MemorySink:
    """An in-memory :class:`Sink` -- the reference consumer for tests / a dry run, APPEND-ONLY by
    key (a row or blob whose key was already stored is skipped, as a real sync would). ``rows`` is
    the table (each row carries its ``_key``); ``blobs`` maps each document's key to its
    ``(content, metadata, keys)``; ``schema`` is the last row schema seen."""

    rows: "list[dict[str, JsonValue]]" = field(default_factory=list)
    blobs: "dict[str, tuple[bytes, dict[str, JsonValue], dict[str, JsonValue]]]" = field(
        default_factory=dict
    )
    schema: "dict[str, str]" = field(default_factory=dict)
    seen: "set[str]" = field(default_factory=set)
    skipped: int = 0  # rows / blobs already held (the append-only skips)

    async def record(
        self, row: "dict[str, JsonValue]", *, key: str, schema: "dict[str, str]"
    ) -> None:
        self.schema = dict(schema)
        if key in self.seen:
            self.skipped += 1
            return
        self.seen.add(key)
        self.rows.append({**row, KEY_COLUMN: key})

    async def blob(
        self,
        content: bytes,
        *,
        key: str,
        keys: "dict[str, JsonValue]",
        content_type: str,
        metadata: "dict[str, JsonValue]",
    ) -> None:
        if key in self.blobs:
            self.skipped += 1
            return
        self.blobs[key] = (content, metadata, keys)


def document_fields(brief: DatasetBrief) -> "set[str]":
    """The brief's fields typed as a downloadable document (per :data:`DOCUMENT_TYPES`)."""
    return {f for f in brief.fields if brief.types.get(f, "").lower() in DOCUMENT_TYPES}


def row_schema(brief: DatasetBrief) -> "dict[str, str]":
    """The row schema a sink receives: every brief field -> its type (``string`` when untyped),
    plus the identity column."""
    schema = {f: (brief.types.get(f) or "string") for f in brief.fields}
    schema[KEY_COLUMN] = "key"
    return schema


def _content_type(url: str) -> str:
    return mimetypes.guess_type(url.split("?")[0])[0] or "application/octet-stream"


def _identity(row: "dict[str, JsonValue]", fields: "list[str]") -> "dict[str, JsonValue]":
    """The row's identity FIELDS (name -> value) -- the part of a document's keys the row gives."""
    return {f: row.get(f) for f in fields}


def _strip_docs(value: JsonValue) -> "tuple[JsonValue, list[dict[str, JsonValue]]]":
    """``value`` with every nested ``_doc`` identity removed, plus those identities (each with the
    fields extracted from that document, as its metadata)."""
    found: list[dict[str, JsonValue]] = []
    if isinstance(value, dict):
        out: dict[str, JsonValue] = {}
        ident = value.get(DOC_COLUMN)
        for k, v in value.items():
            if k == DOC_COLUMN:
                continue
            cleaned, nested = _strip_docs(v)
            out[k] = cleaned
            found.extend(nested)
        if isinstance(ident, dict):
            found.append({**ident, "fields": out})
        return out, found
    if isinstance(value, list):
        items: list[JsonValue] = []
        for v in value:
            cleaned, nested = _strip_docs(v)
            items.append(cleaned)
            found.extend(nested)
        return items, found
    return value, found


async def run_to_sink(
    query: Query, brief: DatasetBrief, sink: Sink, *, resolver: Resolver
) -> "tuple[int, int]":
    """Run ``query`` and route its results to ``sink`` (see the module docstring): each scalar row
    -> ``record`` with its key + schema; each document-typed field's URL, each page the query
    fanned out into, and each bare-URL row -> resolved to a blob -> ``blob`` with its keys and the
    row's scalars as metadata. Returns ``(rows, blobs)`` counts. Pages the query already fetched
    are taken from the run's resolve memo (no refetch); document fetches reuse ``resolver``."""
    doc_fields = document_fields(brief)
    fields = key_fields(Plan.from_blob(query.to_blob())) or list(brief.key)
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
                    content_hash = digest([doc.content.hex()])
                    await sink.blob(
                        doc.content,
                        key=digest([url, content_hash]),
                        keys={"url": url, "hash": content_hash},
                        content_type=_content_type(url),
                        metadata={},
                    )
                    n_blobs += 1
                continue
            cleaned, pages = _strip_docs(item)
            row: dict[str, JsonValue] = cleaned if isinstance(cleaned, dict) else {}
            key_raw = row.pop(KEY_COLUMN, None)
            key = str(key_raw) if isinstance(key_raw, str) else row_key(dict(row), fields)
            scalars: dict[str, JsonValue] = {k: v for k, v in row.items() if k not in doc_fields}
            await sink.record(scalars, key=key, schema=schema)
            n_rows += 1
            identity = _identity(row, fields)
            # every page the query fanned out into is a document too: its stable hash rode on the
            # row; its bytes come from the run's memo (the query just fetched it)
            for page in pages:
                url, content_hash = str(page.get("url") or ""), str(page.get("hash") or "")
                if not url:
                    continue
                doc = await resolver.resolve(url)
                keys: dict[str, JsonValue] = {
                    **identity,
                    "row": key,  # the row it belongs to, whatever the key fields are
                    "url": url,
                    "hash": content_hash,
                }
                await sink.blob(
                    doc.content,
                    key=digest([key, url, content_hash]),
                    keys=keys,
                    content_type=_content_type(url),
                    metadata={**scalars, "fields": page.get("fields")},
                )
                n_blobs += 1
            # resolve each document field's URL to a blob, keeping the row's scalars as its metadata
            for f in doc_fields:
                cell = item.get(f)
                if isinstance(cell, str) and cell.startswith(("http://", "https://")):
                    doc = await resolver.resolve(cell)
                    content_hash = digest([doc.content.hex()])
                    await sink.blob(
                        doc.content,
                        key=digest([key, cell, content_hash]),
                        keys={
                            **identity,
                            "row": key,
                            "field": f,
                            "url": cell,
                            "hash": content_hash,
                        },
                        content_type=_content_type(cell),
                        metadata=scalars,
                    )
                    n_blobs += 1
    return n_rows, n_blobs


__all__ = ["Sink", "MemorySink", "run_to_sink", "document_fields", "row_schema", "DOCUMENT_TYPES"]
