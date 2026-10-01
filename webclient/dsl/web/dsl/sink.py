"""Where a run's results go. A :class:`Sink` is the consumer contract -- ``record`` a row with the
row schema, ``blob`` a document's bytes with its url / content type / metadata (the row it belongs
to). :class:`Dataset` is the default: it collects rows + documents in memory. :class:`MemorySink`
is the append-only reference (dedupes on ``_identity``). Identity is the QUERY's business (see
:mod:`web.dsl.identity`); what to do with it -- dedupe, upsert, version -- is the sink's."""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from pydantic import JsonValue

from .identity import IDENTITY_COLUMN, digest


@runtime_checkable
class Sink(Protocol):
    """A consumer of a run: rows (``record``) and documents (``blob``)."""

    async def record(self, row: "dict[str, JsonValue]", *, schema: "dict[str, str]") -> None: ...
    async def blob(
        self, content: bytes, *, url: str, content_type: str, metadata: "dict[str, JsonValue]"
    ) -> None: ...


def identity_key(row: "dict[str, JsonValue]") -> str:
    """The string a sink dedupes a row on: its ``_identity`` when the query gave it one, else a
    digest of the whole row."""
    ident = row.get(IDENTITY_COLUMN)
    return ident if isinstance(ident, str) and ident else digest([row])


def content_type_of(url: str) -> str:
    return mimetypes.guess_type(url.split("?")[0])[0] or "application/octet-stream"


@dataclass
class Attachment:
    """A fetched document: its url, bytes, content type, and ``metadata`` (the row it belongs to;
    for a page the query fanned out into, the fields read from it)."""

    url: str
    content: bytes
    content_type: str
    metadata: "dict[str, JsonValue]" = field(default_factory=dict)


@dataclass
class Dataset:
    """A run's results in memory: the ROWS (as extracted, ``_identity`` and all) and the DOCUMENTS,
    plus the row ``schema`` (field -> type). The default sink."""

    rows: "list[dict[str, JsonValue]]" = field(default_factory=list)
    documents: "list[Attachment]" = field(default_factory=list)
    schema: "dict[str, str]" = field(default_factory=dict)

    async def record(self, row: "dict[str, JsonValue]", *, schema: "dict[str, str]") -> None:
        self.schema = dict(schema)
        self.rows.append(row)

    async def blob(
        self, content: bytes, *, url: str, content_type: str, metadata: "dict[str, JsonValue]"
    ) -> None:
        self.documents.append(
            Attachment(url=url, content=content, content_type=content_type, metadata=metadata)
        )


@dataclass
class MemorySink:
    """An append-only :class:`Sink`: a row whose identity was already stored is skipped, as is a
    blob whose url + identity was; ``skipped`` counts them."""

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


__all__ = ["Attachment", "Dataset", "MemorySink", "Sink", "content_type_of", "identity_key"]
