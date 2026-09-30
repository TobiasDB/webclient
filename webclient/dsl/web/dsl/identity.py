"""Record IDENTITY -- what makes an extracted row or a fetched document unique, so a query run every
day can be synced append-only (the snapshot model). Identity is IMPLICIT: every extracted row
carries :data:`IDENTITY_COLUMN` -- the hash of its extracted fields; a row read from a document
with nothing extracted hashes the document's content -- and a row read from a document also carries
:data:`URL_COLUMN`, so a sink can store the page. Nested as deep as the data goes: every level has
its own.

``.identity(*parts)`` OVERRIDES it, only when a specific identity is asked for (a brief that says
"the article text identifies a story"): each ``part`` is a FIELD NAME (its value counts) or a CSS
SELECTOR (resolved on the record / the page, its text counts) -- ``.identity("published",
"headline")`` on the records, ``.identity("article")`` on a detail page, so a clock or a sidebar
changing elsewhere does not make a new document. What a sink does with an identity (dedupe, upsert,
version) is the sink's business.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

from web.parse import Document, Element

#: the identity digest column (written by the ``identity`` verb).
IDENTITY_COLUMN = "_identity"
#: the URL a document-derived row was read from (written with its identity, for the sink).
URL_COLUMN = "_url"
#: how many hex chars of the sha256 an identity keeps (96 bits -- collision-safe for a dataset).
_DIGEST_CHARS = 24


def digest(parts: "Sequence[object]") -> str:
    """The sha256 (truncated) of ``parts`` serialised canonically -- one identity function, so the
    same values always give the same identity."""
    blob = json.dumps(list(parts), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]


def _plain(value: object) -> object:
    """A value as it counts toward identity: a nested row keeps only its data (never another
    level's identity / url columns); a list maps through."""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _normalised(text: str) -> str:
    return " ".join(text.split())


def identity_of(
    row: "Mapping[str, object] | None",
    scope: "Document | Element | None" = None,
    parts: "Sequence[str]" = (),
) -> str:
    """The identity of ``row`` (an extracted record) read against ``scope`` (the record's element /
    the document it came from). Each part is a field of the row when the row has it, else a CSS
    selector resolved on the scope (a miss counts as empty); no parts = every data field of the
    row, or -- for a bare document with nothing extracted -- its main content."""
    fields = {k: v for k, v in (row or {}).items() if not str(k).startswith("_")}
    if parts:
        taken: list[object] = []
        for part in parts:
            if part in fields:
                taken.append(("field", part, _plain(fields[part])))
            elif scope is not None:
                el = scope.select(part)
                taken.append(("css", part, _normalised(el.text) if el is not None else ""))
            else:
                taken.append(("field", part, None))
        return digest(taken)
    if fields:
        return digest([(k, _plain(fields[k])) for k in sorted(fields)])
    if isinstance(scope, Document):
        main = scope.main_content()
        return digest([_normalised(main.text if main is not None else scope.text)])
    if isinstance(scope, Element):
        return digest([_normalised(scope.text)])
    return digest([])


__all__ = ["IDENTITY_COLUMN", "URL_COLUMN", "digest", "identity_of"]
