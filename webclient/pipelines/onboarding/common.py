"""Company onboarding: given a BRIEF (a dataset description) and a company, find the
most scrapeable source for that dataset and author a runnable lazy query for it.

The pipeline is seven small stages, each usable on its own; ``onboard_company``
wires them together:

1. :func:`search_web`        -- seed URLs for the company + brief.
2. :func:`crawl_from_seeds`  -- LLM-driven crawl: at each round the model picks the
                                frontier edges most likely to reach the dataset,
                                favouring a *queryable source* (an API over the whole
                                dataset) over a SPA that lists only part of it.
3. :func:`select_candidates` -- rank the crawled pages by scrapability + relevance
                                into must / should / could tiers.
4. :func:`evaluate_candidate`-- for a candidate, read its skeleton + flags and have
                                the model assess the dataset (present? sorted? complete?
                                paginated? filtered? a subset? unstructured? drill-down?).
5. :func:`write_reference`   -- the lazy ``Reference`` for the chosen candidate (its
                                data-API endpoint when the SPA has one, else its URL).
6. :func:`write_resolve`     -- the ``Resolve`` policy (deterministic given the page's
                                ``flags`` -- spa/anti_bot_triggered map to browser/proxy/stealth).

The chosen source then runs a flag-driven decision cascade (in ``onboard_company``):
reference -> the API endpoint if the SPA has one -> a login wall stops it -> the
resolve policy from spa/anti_bot_triggered -> the query, told to page when the
pagination flag is set. So the flags choose the URL, the resolve args, whether a
browser is needed, and whether to follow pagination.
7. :func:`write_query`       -- the lazy web query, authored by the model from the page
                                skeleton against the packaged query spec.

Everything the model does goes through one injected ``llm(prompt) -> str`` callable,
and web search through one injected ``search(query, k) -> list[SearchHit]`` callable,
so the whole pipeline runs offline against a stub model + a local server in tests.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Literal

#: the pipeline's logger. Stages log progress here (seeds, crawl, candidates, the
#: evaluation, the query, spend); a CLI or app sets the level / handler. Each line is
#: also appended to ``OnboardingResult.steps`` for a programmatic trace.
log = logging.getLogger("webclient.pipelines.onboarding")

if TYPE_CHECKING:
    from .artifacts import SearchHit  # noqa: F401


#: the model: a prompt in, its completion text out. Inject any client (a Claude call,
#: a local model, or a stub in tests). Kept deliberately minimal.
LLM = Callable[[str], str]
#: web search: a query + a result count in, ranked hits out.
SearchFn = Callable[[str, int], "list[SearchHit]"]
#: the transport tier for a fetch.
BrowserMode = Literal["never", "auto", "always"]


def _mode(browser: bool) -> BrowserMode:
    return "auto" if browser else "never"


def _fetch(wc: Any, url: str, browser: "BrowserMode", **kw: Any) -> Any:
    """Fetch a page for onboarding. A browser-only site (a bare 403 from e.g. Wikipedia /
    investor.nvidia.com) is escalated to the browser by the ``auto`` ladder itself
    (:data:`webclient.core.client.resolve_loop._BLOCK_STATUSES`) -- the ONE fetch mechanism the
    crawl and every other caller share -- so this is a thin passthrough kept as the single
    onboarding fetch seam (a place to add onboarding-wide fetch policy later)."""
    return wc.fetch(url, browser=browser, **kw)


#: the skeleton line budget for evaluation + query authoring -- generous enough to be
#: the WHOLE structure of essentially any real page (the model needs every record /
#: field), while staying well inside the model's context so an enormous page can't blow
#: it into a 400 "prompt too long".
_FULL_SKELETON = 4000

#: hard CHARACTER budgets for the big, page-derived prompt inputs (~4 chars/token), so
#: no single prompt can grow past the model's context and 400 as "prompt too long".
#: A clipped input keeps the most useful part for its content type (see :func:`_clip`)
#: and notes what was dropped.
_MAX_SKELETON_CHARS = 16_000   # ~4k tokens -- plenty to read a page's structure
_MAX_LISTING_CHARS = 6_000     # the frontier listing for pick_edges
_MAX_PAGES_CHARS = 10_000      # the crawled-pages JSON for select_candidates


def _skeleton_for(doc: Any) -> str:
    """The page skeleton to hand the model, clipped to budget. A big, repetitive page (a
    modern SPA whose records are hundreds of hashed-class siblings -- e.g. a pricing/blog grid
    that server-renders every item) blows past the budget as raw structure, so the record
    pattern is invisible in the clipped view. When the faithful skeleton is too large, re-render
    with ``collapse=True`` -- consecutive STRUCTURALLY-IDENTICAL siblings fold to one
    representative + ``×N`` -- so the repeating record and its fields are legible and the model
    can write a ``select_all`` for it. Small pages keep the faithful, every-sibling view."""
    kind = "json" if doc.kind == "json" else "html"
    skel = doc.skeleton(max_lines=_FULL_SKELETON)
    if len(skel) > _MAX_SKELETON_CHARS:
        # too big to read raw -> fold identical siblings AND drop nav/footer/sidebar chrome so
        # the record region isn't clipped away under menus (hashed classes are always dropped).
        skel = doc.skeleton(max_lines=_FULL_SKELETON, collapse=True, drop_chrome=True)
    return _clip(skel, _MAX_SKELETON_CHARS, "skeleton", kind=kind)


def _clip(text: str, max_chars: int, what: str = "input", *, kind: str = "head") -> str:
    """Keep ``text`` within ``max_chars`` so a huge page can't blow the prompt, trimming
    where the LEAST useful content is for that content type:

    - ``"html"``: keep the CENTRE (the records live in ``<main>``; nav/header/footer are
      chrome at the two ends), so trim evenly from both sides.
    - ``"json"``: keep both ENDS (the shape is at the head and the structure closes at
      the tail; the middle is repetitive array elements), so trim from the centre out.
    - ``"head"`` (default): keep the head (e.g. a best-first link listing).

    A trim leaves a note where content was dropped. Logs at debug when it trims."""
    if len(text) <= max_chars:
        return text
    over = len(text) - max_chars
    note = f"… [trimmed {over} chars of the {what}] …"
    log.debug("clipped %s: %d -> %d chars (%s)", what, len(text), max_chars, kind)
    if kind == "json":  # head + tail (drop the repetitive middle)
        half = max_chars // 2
        return text[:half] + "\n" + note + "\n" + text[-half:]
    if kind == "html":  # the centre (drop the chrome at both ends)
        cut = over // 2
        return note + "\n" + text[cut : cut + max_chars] + "\n" + note
    return text[:max_chars] + "\n" + note  # head


