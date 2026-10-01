"""The hard CHARACTER budget helper every page-derived prompt input goes through: :func:`clip`
trims where the least useful content is for each content type (head / centre / both ends) and
notes what it dropped -- so no prompt can grow past a model's context, and every stage's input
stays tiny. (The stage prompts themselves live in ``pipeline/prompts/``.)
"""

from __future__ import annotations

from typing import Literal

#: hard CHARACTER budgets for the big, page-derived prompt inputs (~4 chars per token), so no
#: prompt can grow past the model's context. A clipped input keeps the most useful part for its
#: content type (see :func:`clip`) and notes what was dropped.
Kind = Literal["head", "html", "json"]


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


__all__ = ["Kind", "clip"]
