"""Author -- ``Reference + DatasetBrief -> a web.dsl (wq) query`` that extracts the LIVE data.

Author is registry-driven, not LLM-driven: it resolves the reference once to get a **sample**,
lets the :mod:`.patterns` registry pick the query SHAPE that fits the structure (a JSON array, a
repeating list, a header table, a file download), then lets the :mod:`.behaviours` registry MODIFY
it from the detection flags (drop empty rows; advise a paginating resolver / an interaction). The
result is a lazy ``wq`` chain -- run it (``await query.acollect(resolver=rs)`` / ``.collect()``),
or ship it (``query.to_blob()`` + :func:`web.dsl.run_blob`) for repeatable, remote runs.

``author`` returns just the query (the deliverable); :func:`build_query` returns it with the
advisory notes and the chosen pattern name, for a caller that wants the whole picture.
"""

from __future__ import annotations

from typing import cast

from pydantic import BaseModel

from web.dsl import LazyCollection
from web.resolve import Resolver

from .behaviours import apply_behaviours
from .models import DatasetBrief, Reference
from .patterns import Query, best_pattern, file_links_query


class Authored(BaseModel):
    """What Author produced: the query as a serialisable ``wq`` blob, the pattern that shaped it,
    and any advisory notes (a pager / interaction the query itself cannot express)."""

    blob: str
    pattern: str
    notes: list[str] = []


async def build_query(reference: Reference, brief: DatasetBrief, *, resolver: Resolver) -> "tuple[Query, str, list[str]]":
    """Build the extraction query and return ``(query, pattern_name, notes)``. Chooses the file-
    links query for a download-listing, else the best-matching pattern over a resolved sample, then
    applies the flag-keyed behaviours. Raises ``ValueError`` if no pattern fits the reference."""
    if brief.download and reference.kind in ("html", "xml"):
        return file_links_query(reference), "file_links", []

    pat = best_pattern(reference)
    if pat is None:
        raise ValueError(f"no pattern fits reference kind={reference.kind!r}")
    sample = await resolver.resolve(reference.url)
    query = pat.build(reference, brief, sample)
    if pat.name == "file_download":  # a single-document download -- no row behaviours apply
        return query, pat.name, []
    rows, notes = apply_behaviours(cast(LazyCollection, query), reference, brief)
    return rows, pat.name, notes


async def author(reference: Reference, brief: "DatasetBrief | None" = None, *, resolver: Resolver) -> "Query":
    """The ``wq`` query that extracts ``reference``'s dataset per ``brief`` (see :func:`build_query`).
    Pass no brief to extract the detected records with default field mapping."""
    query, _pattern, _notes = await build_query(reference, brief or DatasetBrief(), resolver=resolver)
    return query


async def authored(reference: Reference, brief: "DatasetBrief | None" = None, *, resolver: Resolver) -> Authored:
    """Like :func:`author` but returns the serialisable :class:`Authored` (blob + pattern + notes)
    -- what a pipeline stores to re-run the extraction later (via :func:`web.dsl.run_blob`)."""
    query, pattern, notes = await build_query(reference, brief or DatasetBrief(), resolver=resolver)
    return Authored(blob=query.to_blob(), pattern=pattern, notes=notes)


__all__ = ["author", "authored", "build_query", "Authored"]
