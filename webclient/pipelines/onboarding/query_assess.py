"""onboarding.query_assess -- judge an authored query on the THREE axes the pipeline cares
about, from the page's dataset shape (the ``pagination`` / ``filtered`` / ``ordered`` signals,
packaged by ``doc.dataset()``):

  * COMPLETENESS -- does the query capture the WHOLE dataset? (pagination walked, no filter narrowing)
  * CORRECTNESS  -- is what it captured the right set? (unfiltered, and its order known)
  * TIMELINESS   -- are the newest rows present? (computed over the rows by :func:`._timeliness`)

Timeliness lives in :mod:`.dates`; this module adds the shape-derived completeness + correctness
notes, so a "latest" (A) query can be judged for correctness/timeliness and an "all" (B) query for
completeness. The notes are strings for the human + a summary; the booleans gate only where a stage
chooses to."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...core.document.models import DatasetHint


def completeness_note(dataset: "DatasetHint | None", *, paginated: bool) -> "tuple[str, bool]":
    """Does the shipped query cover the whole dataset? ``paginated`` is whether it actually walks the
    pages (a confirmed pager was baked). Returns ``(note, covers_all)``. A single-page listing is
    complete; a paginated one is complete only when the query walks it; an active filter narrows it."""
    if dataset is None:
        return "", True
    if dataset.filtered is not None and dataset.filtered.active:
        act = ", ".join(f"{k}={v}" for k, v in dataset.filtered.active.items())
        return (f"COMPLETENESS: the listing is FILTERED ({act}) -- a SUBSET, so this is not the whole "
                "dataset; drop the filter (the unfiltered recipe) to backfill everything.", False)
    if dataset.paginated is None:
        return "COMPLETENESS: one page -- the whole dataset is on a single page.", True
    best = dataset.paginated.best
    where = best.code if best is not None else "rel=next"
    if paginated:
        return f"COMPLETENESS: the query walks the pagination ({where}) -- all pages captured.", True
    return ("COMPLETENESS: the dataset is PAGINATED but this query captures ONE page (a subset) -- run "
            "the backfill (all-pages) query for the full history.", False)


def correctness_note(dataset: "DatasetHint | None") -> "tuple[str, bool]":
    """Is what the query captured the RIGHT set -- not narrowed by an active filter, and with a known
    order? Returns ``(note, correct)``. ``correct`` is False when an active filter means the visible
    rows are a subset (so a "latest"/"all" query would silently miss records)."""
    if dataset is None:
        return "", True
    bits: list[str] = []
    correct = True
    if dataset.filtered is not None and dataset.filtered.active:
        act = ", ".join(f"{k}={v}" for k, v in dataset.filtered.active.items())
        bits.append(f"an active filter ({act}) narrows the visible rows -- a SUBSET")
        correct = False
    if dataset.ordered is not None and dataset.ordered.key != "unknown":
        o = dataset.ordered
        bits.append(f"ordered by {o.key} {o.direction}"
                    + (" (newest-first: page one IS the latest)" if o.key == "date" and o.direction == "desc" else ""))
    else:
        bits.append("order unknown -- an early stop is not safe; the 'latest' query must be treated with care")
    return "CORRECTNESS: " + "; ".join(bits) + ".", correct
