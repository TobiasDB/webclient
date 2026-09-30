"""Row and document IDENTITY -- what makes a result unique, so a query run daily can be synced
append-only (the snapshot model): a row is identified by its KEY FIELDS, a document by the row's
key fields plus a CONTENT HASH over its stable part.

* :func:`row_key` -- the digest of a row's key fields (or of every scalar column when none are
  declared); the executor's ``key(*fields)`` verb writes it to the row as :data:`KEY_COLUMN`.
* :func:`doc_key` -- a document's stable content hash: the normalised text of a SELECTED element
  (``key("article")`` -- so a clock or a sidebar changing elsewhere on the page does not change the
  hash), else the main content, else the whole text; a JSON document hashes canonically; anything
  else hashes its bytes. The executor writes ``{"url", "hash"}`` as :data:`DOC_COLUMN` on every
  row a fan-out extracts from a resolved document, and ``wq.doc.key(selector)`` reads it as a
  column.
* :func:`key_fields` / :func:`document_selector` -- what a plan DECLARED (its ``key`` step), so a
  sink can name the identity fields without re-deriving them.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from pydantic import JsonValue
from web.parse import Document, Element

from .plan import Plan, Step

#: the row's identity digest column (written by the ``key`` verb).
KEY_COLUMN = "_key"
#: the document identity column a fan-out row carries: ``{"url": ..., "hash": ...}``.
DOC_COLUMN = "_doc"
#: how many hex chars of the sha256 an identity keeps (96 bits -- collision-safe for a dataset).
_DIGEST_CHARS = 24


def digest(parts: "Sequence[object]") -> str:
    """The sha256 (truncated) of ``parts`` serialised canonically -- one identity function for
    rows and documents, so the same values always give the same key."""
    blob = json.dumps(list(parts), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]


def row_key(row: "dict[str, object]", fields: "Sequence[str]" = ()) -> str:
    """A row's identity: the digest of its ``fields`` values in order (a missing field reads as
    ``None``); with no fields, of every non-reserved scalar column (sorted by name)."""
    if fields:
        return digest([_scalar(row.get(f)) for f in fields])
    plain = {k: _scalar(v) for k, v in row.items() if not k.startswith("_")}
    return digest([plain[k] for k in sorted(plain)])


def _scalar(value: object) -> object:
    """A value as it counts toward identity: nested rows / lists collapse to their JSON."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return value


def _normalised(text: str) -> str:
    return " ".join(text.split())


def doc_key(doc: "Document | Element", selector: "str | None" = None) -> str:
    """A document's (or element's) STABLE content hash -- see the module docstring. A ``selector``
    that matches nothing falls back to the document's own stable part."""
    if isinstance(doc, Element):
        el = doc.select(selector) if selector else None
        return digest([_normalised((el or doc).text)])
    if doc.kind == "json":
        return digest([json.dumps(doc.json(), ensure_ascii=False, sort_keys=True, default=str)])
    if not doc._markup():
        return hashlib.sha256(doc.content).hexdigest()[:_DIGEST_CHARS]
    target = doc.select(selector) if selector else None
    if target is None:
        target = doc.main_content()
    text = target.text if target is not None else doc.text
    return digest([_normalised(text)])


def _key_step(plan: Plan) -> "Step | None":
    steps = plan.steps
    for i, s in enumerate(steps):
        if (
            s.kind == "get"
            and s.name == "key"
            and i + 1 < len(steps)
            and steps[i + 1].kind == "call"
        ):
            return steps[i + 1]
    return None


def key_fields(plan: Plan) -> "list[str]":
    """The identity fields a plan DECLARED with ``key(*fields)`` (``[]`` when none)."""
    call = _key_step(plan)
    if call is None:
        return []
    return [str(a.value) for a in call.args if isinstance(a.value, str)]


def document_selector(plan: Plan) -> "str | None":
    """The stable selector a plan declared for its documents' hash (``key(..., document=css)``)."""
    call = _key_step(plan)
    if call is None:
        return None
    arg = call.kwargs.get("document")
    value: JsonValue = arg.value if arg is not None else None
    return value if isinstance(value, str) and value else None


__all__ = [
    "KEY_COLUMN",
    "DOC_COLUMN",
    "digest",
    "row_key",
    "doc_key",
    "key_fields",
    "document_selector",
]
