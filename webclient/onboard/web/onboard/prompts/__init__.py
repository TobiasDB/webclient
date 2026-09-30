"""The pipeline's prompts as DATA -- one ``.md`` :class:`string.Template` per LLM step, shipped as
package data alongside this module, plus the hard CHARACTER BUDGETS every page-derived prompt
input is clipped to.

Keeping the prompts as files (not inline f-strings) lets them be read, diffed and tuned without
touching pipeline code; :func:`render_prompt` loads + renders one (templates are cached after the
first read) and FAILS LOUDLY on a missing or extra placeholder, so a typo in a prompt or a call
site never ships a half-filled prompt.

The budgets are what keep every LLM step CHEAP and bounded: no single prompt can grow past the
model's context (a 400 "prompt too long"), and -- more to the point -- the crawl / select steps
see only metadata, the evaluate + author steps see ONE clipped skeleton. :func:`clip` trims where
the least useful content is for each content type and notes what it dropped.
"""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files
from string import Template
from typing import Literal

#: hard CHARACTER budgets for the big, page-derived prompt inputs (~4 chars per token), so no
#: prompt can grow past the model's context. A clipped input keeps the most useful part for its
#: content type (see :func:`clip`) and notes what was dropped.
MAX_SKELETON_CHARS = 16_000  # ~4k tokens -- plenty to read a page's structure (evaluate / author)
MAX_LISTING_CHARS = 6_000  # the frontier listing for pick_edges / the seed listing for verify_seeds
MAX_PAGES_CHARS = 10_000  # the crawled-pages JSON for select_candidates

Kind = Literal["head", "html", "json"]


@lru_cache(maxsize=None)
def _template(name: str) -> Template:
    """The cached :class:`string.Template` for ``<name>.md`` in this package."""
    text = files(__name__).joinpath(f"{name}.md").read_text(encoding="utf-8")
    # drop the file's trailing newline so a rendered prompt matches the exact wording of a
    # hand-written string (editors add one; the prompt does not want it).
    return Template(text.rstrip("\n"))


def render_prompt(name: str, /, **variables: str) -> str:
    """Render prompt ``name`` (a ``.md`` file in this package) with ``variables``. Every
    ``$placeholder`` in the template must be supplied; a missing one raises (``KeyError`` via
    :meth:`string.Template.substitute`), so a typo in a prompt or a call site fails loudly."""
    return _template(name).substitute(variables)


def clip(text: str, max_chars: int, what: str = "input", *, kind: Kind = "head") -> str:
    """Keep ``text`` within ``max_chars`` so a huge page can't blow the prompt, trimming where the
    LEAST useful content is for that content type:

    - ``"html"``: keep the CENTRE (the records live in ``<main>``; nav/header/footer are chrome at
      the two ends), so trim evenly from both sides.
    - ``"json"``: keep both ENDS (the shape is at the head and the structure closes at the tail;
      the middle is repetitive array elements), so trim from the centre out.
    - ``"head"`` (default): keep the head (e.g. a best-first link listing).

    A trim leaves a note where content was dropped."""
    if len(text) <= max_chars:
        return text
    over = len(text) - max_chars
    note = f"… [trimmed {over} chars of the {what}] …"
    if kind == "json":  # head + tail (drop the repetitive middle)
        half = max_chars // 2
        return text[:half] + "\n" + note + "\n" + text[-half:]
    if kind == "html":  # the centre (drop the chrome at both ends)
        cut = over // 2
        return note + "\n" + text[cut : cut + max_chars] + "\n" + note
    return text[:max_chars] + "\n" + note  # head


__all__ = [
    "render_prompt",
    "clip",
    "MAX_SKELETON_CHARS",
    "MAX_LISTING_CHARS",
    "MAX_PAGES_CHARS",
]
