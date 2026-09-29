"""Patterns -- the example-based registry Author is PRIMARILY driven by.

A **Pattern** maps a well-known structure (a JSON array-of-objects, a repeating-record list, a
header table, a file listing) to its corresponding ``wq`` query SHAPE. Each is a structural
:class:`Pattern` (``match`` scores how well it fits a :class:`Reference`; ``build`` emits the
``wq`` chain, inspecting a resolved ``sample`` to map the brief's field NAMES onto concrete
sub-selectors / JSON paths). Registering another is one ``@pattern`` -- no if-chain to edit.

Field mapping is deterministic: a field name resolves to a record sub-selector by class /
``itemprop`` / ``data-*`` (HTML) or a matching key (JSON); an explicit ``brief.selectors`` entry
always wins. HTML and JSON use the SAME verbs -- ``select_all(row).extract(field=wq.doc.select(...)
.attr("text"))`` -- only the selector differs (a CSS selector for markup, a dotted path for JSON).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import JsonValue

from web.dsl import LazyCollection, LazyDocument, LazyField, LazyValues, wq
from web.parse import Document as Page, Element, dig

from .models import DatasetBrief, Reference

#: any recorded query Author can emit (rows / a scalar fan-out / a single document download).
Query = LazyCollection | LazyValues | LazyDocument

#: envelope keys a data-API commonly wraps its record array in, tried before a blind scan.
_ENVELOPES = ("results", "data", "items", "rows", "records", "entries", "docs", "hits")
#: field names that denote a LINK -- mapped to the row's ``<a href>`` rather than its text.
_LINKISH = frozenset({"url", "link", "href", "source", "page", "detail", "profile"})
#: file extensions the ``file_download`` pattern harvests from a listing.
_FILE_EXT = ("pdf", "xlsx", "xls", "csv", "doc", "docx", "zip")


def _is_table_row(selector: "str | None") -> bool:
    """Whether a detected record selector is a table row (``tr`` / ``tbody`` ...) -- those rows
    carry no per-field classes, so the header-column pattern maps them, not the class pattern."""
    return bool(selector) and (selector or "").split(".")[0].split(" ")[-1] in ("tr", "tbody")


@runtime_checkable
class Pattern(Protocol):
    """A structure -> query rule. ``match`` returns 0..1 (0 = does not apply); ``build`` emits the
    ``wq`` chain, mapping ``brief``'s fields onto ``sample``'s shape."""

    name: str

    def match(self, reference: Reference) -> float: ...
    def build(self, reference: Reference, brief: DatasetBrief, sample: Page) -> "LazyCollection | LazyDocument": ...


_PATTERNS: list[Pattern] = []


def pattern(cls: "type[Pattern]") -> "type[Pattern]":
    """Register a Pattern class (instantiated no-arg) into the registry."""
    _PATTERNS.append(cls())
    return cls


def best_pattern(reference: Reference) -> "Pattern | None":
    """The registered pattern that fits ``reference`` best (highest ``match``), or ``None``."""
    scored = [(p.match(reference), p) for p in _PATTERNS]
    scored = [(s, p) for s, p in scored if s > 0.0]
    return max(scored, key=lambda sp: sp[0])[1] if scored else None


# -- field mapping (deterministic; an explicit brief.selectors entry wins) --

def html_field_selector(record: Element, name: str) -> "str | None":
    """A record sub-selector for field ``name``: a link field -> ``a@href``; else the first of
    ``.name`` / ``[itemprop=name]`` / ``[data-name]`` / the tag that matches inside the sample record."""
    if name.lower() in _LINKISH and record.select("a[href]") is not None:
        return "a@href"
    for sel in (f".{name}", f"[itemprop={name}]", f"[data-{name}]", name):
        if record.select(sel) is not None:
            return sel
    return None


def json_field_path(obj: "dict[str, JsonValue]", name: str) -> str:
    """A dotted JSON path for field ``name`` in a sample object: a case-insensitive top-level key,
    else a one-level-nested key (``address.city``), else the name itself (a best-effort path)."""
    low = name.lower()
    for k in obj:
        if k.lower() == low:
            return k
    for k, v in obj.items():
        if isinstance(v, dict):
            for kk in v:
                if kk.lower() == low:
                    return f"{k}.{kk}"
    return name


def _html_col(spec: str) -> LazyField:
    """A ``wq`` column expression from a record sub-selector: ``"css"`` -> its text; ``"css@attr"``
    -> that attribute; ``"@attr"`` / an empty css -> the row element itself."""
    css, sep, attr = spec.partition("@")
    node: LazyDocument = wq.doc.select(css) if css else wq.doc
    return node.attr(attr) if sep else node.attr("text")


def _json_col(path: str) -> LazyField:
    """A ``wq`` column expression for a JSON leaf at a dotted ``path`` (the JSON twin of :func:`_html_col`)."""
    return wq.doc.select(path).attr("text")


def _html_columns(record: Element, brief: DatasetBrief) -> "dict[str, LazyField]":
    cols: dict[str, LazyField] = {}
    for name in brief.fields:
        spec = brief.selectors.get(name) or html_field_selector(record, name)
        if spec is not None:
            cols[name] = _html_col(spec)
    return cols or {"text": wq.doc.attr("text")}  # no field mapped -> the record's text


def _json_array_path(value: JsonValue) -> "str | None":
    """The dotted path to the record ARRAY in a JSON value: the root if it is a list of objects,
    else an envelope key, else the first key holding a list of objects, else one level nested."""
    if isinstance(value, list):
        return "" if value and isinstance(value[0], dict) else None
    if isinstance(value, dict):
        for k in (*_ENVELOPES, *value.keys()):
            v = value.get(k)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return k
        for k, v in value.items():
            if isinstance(v, dict) and (inner := _json_array_path(v)) is not None:
                return f"{k}.{inner}" if inner else k
    return None


@pattern
class JsonArray:
    """A JSON data-API / feed: an array of objects, possibly under an envelope key. ->
    ``reference(url).resolve().select_all(path).extract(**fields)`` (``select_all`` navigates the
    JSON array by path; each column reads a leaf)."""

    name = "json_array"

    def match(self, reference: Reference) -> float:
        return 1.0 if reference.kind == "json" else 0.0

    def build(self, reference: Reference, brief: DatasetBrief, sample: Page) -> LazyCollection:
        value = sample.json()
        path = _json_array_path(value) or ""
        node = dig(value, path)
        first = node[0] if isinstance(node, list) and node and isinstance(node[0], dict) else {}
        cols = {name: _json_col(brief.selectors.get(name) or json_field_path(first, name))
                for name in brief.fields} or {"value": wq.doc.attr("text")}
        return wq.reference(reference.url).resolve().select_all(path).extract(**cols)


@pattern
class RepeatingRecords:
    """A repeating-record HTML list (the dominant dataset region). ->
    ``reference(url).resolve().select_all(row).extract(**fields)``."""

    name = "repeating_records"

    def match(self, reference: Reference) -> float:
        if reference.kind != "html" or not reference.record_selector:
            return 0.0
        return 0.3 if _is_table_row(reference.record_selector) else 0.8

    def build(self, reference: Reference, brief: DatasetBrief, sample: Page) -> LazyCollection:
        row = reference.record_selector or ""
        records = sample.select_all(row)
        cols = _html_columns(records[0], brief) if records else {"text": wq.doc.attr("text")}
        return wq.reference(reference.url).resolve().select_all(row).extract(**cols)


@pattern
class HtmlTable:
    """A header table: header cells name the columns; each body row is a record. Maps each field to
    its ``td`` column by header text -> ``select_all("table tr").extract(field=td:nth-child(k))``."""

    name = "html_table"

    def match(self, reference: Reference) -> float:
        if reference.kind != "html":
            return 0.0
        return 0.9 if _is_table_row(reference.record_selector) else 0.4

    def build(self, reference: Reference, brief: DatasetBrief, sample: Page) -> LazyCollection:
        headers = [h.text.strip().lower() for h in sample.select_all("table th")]
        cols: dict[str, LazyField] = {}
        for name in brief.fields:
            override = brief.selectors.get(name)
            if override is not None:
                cols[name] = _html_col(override)
            elif name.lower() in headers:
                cols[name] = wq.doc.select(f"td:nth-child({headers.index(name.lower()) + 1})").attr("text")
        return wq.reference(reference.url).resolve().select_all("table tr").extract(
            **(cols or {"text": wq.doc.attr("text")}))


@pattern
class FileDownload:
    """Content downloads: a binary reference IS the file -> ``reference(url).resolve()`` fetches the
    document (a PDF / spreadsheet / HTML blob) for the caller to save."""

    name = "file_download"

    def match(self, reference: Reference) -> float:
        return 0.9 if reference.kind not in ("html", "json", "xml") else 0.0

    def build(self, reference: Reference, brief: DatasetBrief, sample: Page) -> LazyDocument:
        return wq.reference(reference.url).resolve()


def file_links_query(reference: Reference) -> LazyValues:
    """The query for a LISTING of downloadable files (used when ``brief.download`` is set on an
    HTML page): every same-page link ending in a known file extension, resolved absolute."""
    sel = ", ".join(f"a[href$='.{ext}']" for ext in _FILE_EXT)
    return wq.reference(reference.url).resolve().select_all(sel).attr("href")


__all__ = ["Pattern", "pattern", "best_pattern", "html_field_selector", "json_field_path",
           "file_links_query", "Query", "JsonArray", "RepeatingRecords", "HtmlTable", "FileDownload"]
