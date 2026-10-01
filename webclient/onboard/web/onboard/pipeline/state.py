"""The stage CONTRACTS and the resumable onboarding STATE.

Each stage returns one model below; the :class:`Onboarding` state holds one slot per stage in
order. A slot that is ``None`` is work still to do -- that is how a run resumes. The state is plain
JSON (``save`` / ``load``): a crashed run, a budget stop or a deliberate ``reset_from(stage)`` all
continue from the first empty slot.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, JsonValue

from .brief import Brief

Tier = Literal["must", "could", "lead"]

# -- 1. search ------------------------------------------------------------------------------------


class Hit(BaseModel):
    """One search result, scored by the brief's hints: ``domain`` / ``path`` list the fragments
    that matched; ``score`` is their weighted sum (a domain match counts double)."""

    url: str
    title: str = ""
    snippet: str = ""
    score: float = 0.0
    domain: list[str] = []
    path: list[str] = []


class SearchResult(BaseModel):
    term: str
    hits: list[Hit] = []


# -- 2. review search / 3. crawl -----------------------------------------------------------------


class Pick(BaseModel):
    """A URL worth following: ``must`` = this IS the dataset listing (evaluate at once), ``could``
    = likely holds it (evaluate if no must is accepted), ``lead`` = links towards it (crawl)."""

    url: str
    tier: Tier = "could"
    why: str = ""
    score: float = 0.0


class SearchReview(BaseModel):
    picks: list[Pick] = []
    dropped: list[str] = []  # the hits the model left out


class Visited(BaseModel):
    """A page the crawl fetched: its status, the flags that fired, the record count, and the tier
    the review gave the link that led there (``""`` for a seed / an unreviewed link)."""

    url: str
    status: int = 0
    ok: bool = False
    kind: str = "html"
    flags: list[str] = []
    tier: str = ""
    score: float = 0.0


class CandidateReview(BaseModel):
    """Stage 4's verdict on one candidate page: the dataset is ``present`` or not, at the
    ``profile`` (transport tier) that was used; ``retried_browser`` says the HTTP tier showed
    nothing and a browser render was tried."""

    url: str
    present: bool = False
    profile: str = "basic"
    reason: str = ""
    retried_browser: bool = False
    kind: str = "html"


class CrawlResult(BaseModel):
    """What the crawl saw and what to evaluate next: ``candidates`` in evaluation order (accepted
    musts first, then coulds by score); ``reviews`` are the musts reviewed DURING the crawl (so a
    resumed stage 4 never pays for them again); ``stopped_early`` when an accepted must ended it."""

    visited: list[Visited] = []
    candidates: list[Pick] = []
    reviews: list[CandidateReview] = []
    stopped_early: bool = False
    note: str = ""


# -- 5. expand -------------------------------------------------------------------------------------


class PaginateDescription(BaseModel):
    kind: str = ""  # next_link | param | cursor | scroll
    next_selector: str = ""
    param: str = ""
    note: str = ""


class ApiDescription(BaseModel):
    url: str
    kind: str = "json"
    records_path: str = ""  # the JSON path to the record array
    fit: int = 0  # how many brief fields its keys resemble
    knobs: dict[str, str] = {}  # the endpoint's query parameters as called (page / year / type…)


class SpaDescription(BaseModel):
    profile: str = "full_browser"
    reason: str = ""


class DatasetSource(BaseModel):
    """THE contract between locating and authoring: where the dataset is and how it LOADS -- the
    tier, the pager, the feed, the filters, the app -- each a DESCRIPTION from the page's signals.
    Never a selector: what to select is the author's call over the document."""

    url: str
    kind: str = "html"
    profile: str = "basic"
    flags: list[str] = []
    pagination: "PaginateDescription | None" = None
    api: "ApiDescription | None" = None
    ordered: str = ""  # newest-first | oldest-first | "" (unknown)
    filtered: bool = False
    #: the FILTER / TAB controls the page shows (described, never selected for the author): e.g.
    #: "year tabs: 2026 (selected), 2025, 2024 -- older years sit in a different container"
    filters: list[str] = []
    spa: "SpaDescription | None" = None
    detail: dict[str, JsonValue] = {}


class LocationReview(BaseModel):
    ok: bool = False
    summary: str = ""
    concerns: list[str] = []


# -- 7..9 author -----------------------------------------------------------------------------------


class ResolvePlan(BaseModel):
    """The request that returns the dataset's document: ``source`` is the ``wq`` chain."""

    url: str
    profile: str = "basic"
    via_api: bool = False
    source: str = ""


class ExtractQuery(BaseModel):
    """The field extraction over the resolved document for ONE authoring guide -- the ``wq``
    source (page-relative) and the self-contained ``blob``; ``sample`` rows; ``misses`` are
    required fields never populated."""

    name: str = ""  # the guide's name ("" for a single-query brief)
    source: str = ""
    blob: str = ""
    record_selector: str = ""
    fields: dict[str, str] = {}  # field -> the column chain the model wrote (normalised)
    row_count: int = 0
    sample: list[JsonValue] = []
    misses: list[str] = []
    attempts: list[str] = []
    report: str = ""  # the final run's report summary (web.dsl.Report.summary())
    complete: bool = False


class AuthorReview(BaseModel):
    """The final review -- and the NESTED seam: ``next`` is ``"nested"`` when required fields are
    not on the listing but every record links to its own page (``detail_field`` names the URL
    column; ``pending`` the fields to read there): the detail pages are onboarded as their own
    source from stage 7, with ``detail_field``'s URLs as the records, and the queries joined."""

    name: str = ""  # the guide reviewed
    ok: bool = False
    notes: str = ""
    next: str = "done"  # done | nested
    detail_field: str = ""
    pending: list[str] = []


# -- the state --------------------------------------------------------------------------------------


#: how much of a prompt / reply the state keeps per exchange (the whole reply is what a human
#: needs to see why a stage failed; a prompt is reproducible from the state's own values).
TRACE_CHARS = 6_000


class Trace(BaseModel):
    """One model exchange kept on the state: the stage, the prompt sent and the reply received
    (each clipped to :data:`TRACE_CHARS`) -- so ``state.json`` shows exactly what the model saw
    and said at every step."""

    stage: str
    prompt: str = ""
    reply: str = ""


class StageLog(BaseModel):
    stage: str
    started: str
    elapsed_s: float = 0.0
    calls: int = 0
    usd: float = 0.0
    chars_in: int = 0
    chars_out: int = 0
    note: str = ""


class Spend(BaseModel):
    """The run's model spend: the client's OWN metered ``usd`` (the shim's accounting includes its
    CLI overhead), and the prompt / reply sizes every call sent, from which :meth:`api_estimate`
    prices the same calls on the API (Haiku list prices) -- the number the $0.01 target is about."""

    calls: int = 0
    usd: float = 0.0
    chars_in: int = 0
    chars_out: int = 0
    by_stage: dict[str, float] = {}

    def api_estimate(self, usd_in: float = 1.0, usd_out: float = 5.0) -> float:
        """The API cost of this run's calls at ``usd_in`` / ``usd_out`` per million tokens
        (4 chars ~ 1 token), plus a fixed system prompt per call."""
        tokens_in = self.chars_in / 4 + 60 * self.calls
        return round(tokens_in * usd_in / 1e6 + (self.chars_out / 4) * usd_out / 1e6, 5)


class Onboarding(BaseModel):
    """The whole onboarding as data: the rendered brief, one slot per stage (``None`` = to do),
    the spend, the per-stage log, and a ``stopped`` reason when a stage ended the run."""

    version: int = 1
    brief: Brief
    search: "SearchResult | None" = None
    review_search: "SearchReview | None" = None
    crawl: "CrawlResult | None" = None
    review_candidate: "CandidateReview | None" = None
    expand: "DatasetSource | None" = None
    review_location: "LocationReview | None" = None
    author_resolve: "ResolvePlan | None" = None
    author_extract: "list[ExtractQuery] | None" = None  # one per authoring guide
    author_review: "list[AuthorReview] | None" = None  # one per query, same order
    spend: Spend = Spend()
    log: list[StageLog] = []
    trace: list[Trace] = []
    stopped: str = ""
    #: the review -> repair loop: how many times the author review sent the extraction back, and
    #: the reviewer's note the next extraction attempt opens with.
    repairs: int = 0
    review_note: str = ""

    @classmethod
    def start(cls, brief: Brief, **values: str) -> "Onboarding":
        """A fresh state for ``brief`` rendered with its argument ``values``."""
        return cls(brief=brief.render(**values))

    def save(self, path: "str | Path") -> Path:
        p = Path(path)
        p.write_text(self.model_dump_json(indent=1), encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: "str | Path") -> "Onboarding":
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))

    def reset_from(self, stage: str) -> None:
        """Clear ``stage`` and every later stage's output (and the stop), so a run redoes them."""
        names = STAGE_NAMES
        if stage not in names:
            raise KeyError(f"unknown stage {stage!r}; stages are {', '.join(names)}")
        for name in names[names.index(stage) :]:
            setattr(self, name, None)
        self.stopped = ""

    def next_stage(self) -> "str | None":
        """The first stage without an output, or ``None`` when the onboarding is complete."""
        if self.stopped:
            return None
        return next((n for n in STAGE_NAMES if getattr(self, n) is None), None)

    def charge(
        self, stage: str, calls: int, usd: float, *, chars_in: int = 0, chars_out: int = 0
    ) -> None:
        self.spend.calls += calls
        self.spend.usd += usd
        self.spend.chars_in += chars_in
        self.spend.chars_out += chars_out
        self.spend.by_stage[stage] = self.spend.by_stage.get(stage, 0.0) + usd


#: the stages in order -- the state's slots.
STAGE_NAMES: "tuple[str, ...]" = (
    "search",
    "review_search",
    "crawl",
    "review_candidate",
    "expand",
    "review_location",
    "author_resolve",
    "author_extract",
    "author_review",
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = [
    "ApiDescription",
    "AuthorReview",
    "CandidateReview",
    "CrawlResult",
    "DatasetSource",
    "ExtractQuery",
    "Hit",
    "LocationReview",
    "Onboarding",
    "PaginateDescription",
    "Pick",
    "ResolvePlan",
    "SearchResult",
    "SearchReview",
    "SpaDescription",
    "Spend",
    "STAGE_NAMES",
    "StageLog",
    "TRACE_CHARS",
    "Tier",
    "Trace",
    "Visited",
]
