"""Patterns -- the NATURAL-LANGUAGE query-writing knowledge the onboard tier consults.

A **pattern** here is no longer a hardcoded Python class: it is knowledge, written as markdown
(:download:`patterns.md`) -- well-known HTML / JSON / XML structures and the ``wq`` query that
extracts each, with worked, verified examples. :data:`PATTERNS_GUIDE` is that markdown, and
:func:`author_prompt` renders it into the prompt alongside the page's hardcoded
:mod:`Signals/Flags <web.resolve>` (the well-known indicators that modify interpretation) and a
token-lean page skeleton. The model reads all three and writes the ``wq`` chain
(:func:`~web.onboard.compile.parse_query` rebuilds it).

Signals/Flags stay hardcoded because they are ground truth about the page (a login wall, a pager,
a JSON data-API); the STRUCTURE-to-query mapping is knowledge that reads and evolves far better as
prose + examples than as an if-chain of pattern classes.
"""

from __future__ import annotations

from importlib.resources import files

from web.resolve import Flag

from .models import DatasetBrief

#: the query-writing guide (markdown + worked examples), rendered into every author prompt.
PATTERNS_GUIDE: str = (
    files("web.onboard").joinpath("patterns.md").read_text(encoding="utf-8")
)


def _flags_line(flags: "list[Flag]") -> str:
    """The page's fired flags as one advisory block: each present flag, its remedy, and the evidence
    signals behind it -- the hardcoded indicators the model must account for (a pager it should not
    try to express, a JSON data-API to read as JSON, a login wall, ...)."""
    present = [f for f in flags if f.present]
    if not present:
        return "PAGE SIGNALS: none fired (a plain static page)."
    lines = [
        f"  - {f.name} (remedy: {f.remedy}; evidence: "
        f"{', '.join(s.name for s in f.signals) or 'n/a'})"
        for f in present
    ]
    return (
        "PAGE SIGNALS (hardcoded detections about this page -- account for them):\n"
        + "\n".join(lines)
    )


def _fields_line(brief: DatasetBrief) -> str:
    """The requested fields as a schema line: each field with any explicit selector override, its
    description (what the field is), and an ``(optional)`` marker so the model writes
    ``select(..., optional=True)`` for fields that may legitimately be absent."""
    if not brief.fields:
        return "Fields: (none given -- extract the salient fields of each record)."
    parts: list[str] = []
    for name in brief.fields:
        piece = f"{name} [{brief.selectors[name]}]" if name in brief.selectors else name
        if name in brief.descriptions:
            piece += f" -- {brief.descriptions[name]}"
        if name in brief.optional:
            piece += " (optional)"
        parts.append(piece)
    return "Fields (the columns each record should carry): " + "; ".join(parts)


def author_prompt(
    brief: DatasetBrief, skeleton: str, flags: "list[Flag]", *, kind: str
) -> str:
    """The prompt that asks the model to write the extraction query: the patterns guide, the ask
    (goal + fields), the page's hardcoded signals/flags, and the page skeleton. ``kind`` is the
    document kind (``json`` steers the model to the dotted-path form)."""
    goal = brief.goal or "the repeating dataset on this page"
    pager = (
        "\nThis page is PAGINATED -- write the query for ONE page exactly as normal; the "
        "pipeline follows the pagination. Do NOT add a 'next' field."
        if any(f.name in ("paginated", "infinite_scroll") and f.present for f in flags)
        else ""
    )
    kind_note = (
        "\nThis is a JSON document -- use dotted paths in select/select_all and read keys "
        'with .attr("<key>").'
        if kind == "json"
        else ""
    )
    hints = f"\nAuthor guidance: {brief.hints}" if brief.hints else ""
    return (
        f"{PATTERNS_GUIDE}\n\n"
        "----\n"
        f"Using ONLY the query syntax above, write ONE wq query that extracts this dataset: {goal}.\n"
        f"{_fields_line(brief)}{kind_note}{pager}{hints}\n\n"
        f"{_flags_line(flags)}\n\n"
        "Base your selectors on this page skeleton (a token-lean DOM/JSON outline; a RECORD LIST "
        f"mark shows a likely row selector):\n{skeleton}\n\n"
        "Reply with ONLY the query expression -- the wq.doc... chain itself, no prose, no code fence."
    )


__all__ = ["PATTERNS_GUIDE", "author_prompt"]
