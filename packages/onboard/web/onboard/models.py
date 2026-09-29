"""The typed value models the Locate/Author split flows through -- pure data, no behaviour.

Three models, one per boundary:
  * :class:`LocateBrief` -- the *locate*-brief: what dataset to find (and any shortcuts).
  * :class:`Reference`   -- Locate's output / Author's input: WHERE the dataset is, plus hints.
  * :class:`DatasetBrief`-- the *dataset*-brief: the fields/shape wanted (and any overrides).

Keeping these as small pydantic models (not ad-hoc dicts) is what lets Locate and Author be
independent, testable units: a Reference produced by hand exercises Author with no crawl, and a
Reference produced by Locate is inspectable without running Author.
"""

from __future__ import annotations

from pydantic import BaseModel, JsonValue


class LocateBrief(BaseModel):
    """What dataset to FIND. ``goal`` is the free-text ask. Supply ``seeds`` to skip web search,
    or ``candidates`` to skip crawling too (evaluate exactly these). ``max_pages`` bounds the
    crawl; ``prefer_api`` toggles the XHR/data-API preference rule (on by default)."""

    goal: str = ""
    seeds: list[str] = []
    candidates: list[str] = []
    max_pages: int = 20
    prefer_api: bool = True


class DatasetBrief(BaseModel):
    """The SHAPE wanted. ``fields`` are the record fields (by name); ``selectors`` overrides the
    auto-mapped sub-selector/JSON-path for any field (name -> css or dotted path); ``download``
    asks for the file(s) themselves (a PDF/Excel/HTML blob) rather than parsed rows."""

    goal: str = ""
    fields: list[str] = []
    selectors: dict[str, str] = {}
    download: bool = False


class Reference(BaseModel):
    """WHERE the located dataset is, plus the hints Author needs. ``url`` is the source to query
    -- the **XHR/data-API endpoint when one backs the page** (JSON beats HTML), else the page
    itself; ``page_url`` is always the page it was found on. ``kind`` is the sniffed kind of
    ``url``. ``flags``/``signals`` are the conclusions/evidence that fired (from web.resolve).
    ``record_selector`` is the suggested repeating-row ``select_all`` target; ``pagination`` is
    the pager remedy (``paginate`` / ``paginate:scroll`` / ``paginate:cursor``); ``needs_browser``
    means a static fetch won't build the DOM; ``api_endpoint`` is the discovered data-API (equals
    ``url`` when preferred). ``detail`` carries any extra evidence."""

    url: str
    kind: str = "html"
    page_url: str = ""
    flags: list[str] = []
    signals: list[str] = []
    record_selector: "str | None" = None
    pagination: "str | None" = None
    needs_browser: bool = False
    api_endpoint: "str | None" = None
    detail: dict[str, JsonValue] = {}


__all__ = ["LocateBrief", "DatasetBrief", "Reference"]
