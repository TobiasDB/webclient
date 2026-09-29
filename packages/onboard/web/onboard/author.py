"""Author -- ``Reference + DatasetBrief -> a web.dsl (wq) query`` that extracts the LIVE data.

Author is LLM-driven, over natural-language knowledge (not a hardcoded pattern registry): it
resolves the reference once for a **sample**, gathers the page's hardcoded
:mod:`Signals/Flags <web.resolve>`, and hands the model (a) those signals/flags and (b) the
:mod:`patterns <web.onboard.patterns>` guide -- markdown + worked ``wq`` examples for the
well-known HTML/JSON/XML structures. The model writes the ``wq`` chain;
:func:`~web.onboard.compile.parse_query` rebuilds it safely and :func:`~web.onboard.compile.reroot`
roots it at the source. The flag-keyed :mod:`.behaviours` then add advisory notes (a pager /
interaction the static query cannot express). A binary reference / a download brief is handled
deterministically -- no model needed to say "fetch the file".

``author`` returns just the query (the deliverable); :func:`build_query` returns it with the
engine that produced it and the advisory notes, for a caller that wants the whole picture.
"""

from __future__ import annotations

from typing import cast

from pydantic import BaseModel
from web.dsl import LazyCollection, wq
from web.parse import Document
from web.resolve import Resolver, flags

from .behaviours import apply_behaviours
from .compile import Query, parse_query, reroot
from .llm import Llm
from .models import DatasetBrief, Reference
from .patterns import author_prompt

#: file extensions a download-listing query harvests from an HTML page.
_FILE_EXT = ("pdf", "xlsx", "xls", "csv", "doc", "docx", "zip")
#: document kinds that carry an extractable structure (anything else IS the file to download).
_STRUCTURED = frozenset({"html", "xml", "json"})


class Authored(BaseModel):
    """What Author produced: the query as a serialisable ``wq`` blob, the engine that wrote it
    (``llm`` / ``file_links`` / ``file_download``), and any advisory notes (a pager / interaction
    the query itself cannot express)."""

    blob: str
    engine: str
    notes: list[str] = []


def _file_links_query(url: str) -> Query:
    """A LISTING of downloadable files on an HTML page: every same-page link ending in a known file
    extension, resolved absolute (used when ``brief.download`` is set on an HTML/XML page).
    """
    selector = ", ".join(f"a[href$='.{ext}']" for ext in _FILE_EXT)
    return cast(Query, wq.reference(url).resolve().select_all(selector).attr("href"))


def _skeleton(sample: Document) -> str:
    """A token-lean outline of the sample for the prompt: the JSON shape for a JSON document, else
    the record-marked DOM skeleton with page chrome dropped."""
    if sample.kind == "json":
        return sample.json_skeleton(max_lines=200)
    return sample.skeleton(max_lines=200, drop_chrome=True)


async def build_query(
    reference: Reference, brief: DatasetBrief, *, resolver: Resolver, llm: Llm
) -> "tuple[Query, str, list[str]]":
    """Build the extraction query and return ``(query, engine, notes)``. A download brief on an
    HTML/XML page yields the file-links query; a binary reference yields a plain fetch; otherwise
    the model writes the ``wq`` chain over the patterns guide + the page's signals/flags, and the
    flag-keyed behaviours add advisory notes. Raises :class:`~web.onboard.compile.QueryError` if the
    model's reply is not a rebuildable ``wq`` chain."""
    if brief.download and reference.kind in ("html", "xml"):
        return _file_links_query(reference.url), "file_links", []
    sample = await resolver.resolve(reference.url)
    if (
        sample.kind not in _STRUCTURED
    ):  # a PDF / spreadsheet / blob IS the dataset -- fetch it
        return cast(Query, wq.reference(reference.url).resolve()), "file_download", []
    prompt = author_prompt(brief, _skeleton(sample), flags(sample), kind=sample.kind)
    query = reroot(parse_query(await llm.complete(prompt)), reference.url)
    rows, notes = apply_behaviours(
        cast(LazyCollection[object], query), reference, brief
    )
    return cast(Query, rows), "llm", notes


async def author(
    reference: Reference,
    brief: "DatasetBrief | None" = None,
    *,
    resolver: Resolver,
    llm: Llm,
) -> Query:
    """The ``wq`` query that extracts ``reference``'s dataset per ``brief`` (see :func:`build_query`).
    Pass no brief to extract the salient record fields with default guidance."""
    query, _engine, _notes = await build_query(
        reference, brief or DatasetBrief(), resolver=resolver, llm=llm
    )
    return query


async def authored(
    reference: Reference,
    brief: "DatasetBrief | None" = None,
    *,
    resolver: Resolver,
    llm: Llm,
) -> Authored:
    """Like :func:`author` but returns the serialisable :class:`Authored` (blob + engine + notes)
    -- what a pipeline stores to re-run the extraction later (via :func:`web.dsl.run_blob`).
    """
    query, engine, notes = await build_query(
        reference, brief or DatasetBrief(), resolver=resolver, llm=llm
    )
    return Authored(blob=query.to_blob(), engine=engine, notes=notes)


__all__ = ["author", "authored", "build_query", "Authored"]
