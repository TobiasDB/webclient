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
from .prompts import render_prompt

#: the query-writing guide (markdown + worked examples), rendered into every author prompt.
PATTERNS_GUIDE: str = files("web.onboard").joinpath("patterns.md").read_text(encoding="utf-8")

# Split the guide into the always-included PREAMBLE (core syntax) and the numbered worked EXAMPLES,
# so a prompt carries only the examples relevant to THIS page -- the model gets the right worked
# example without the whole guide blowing up the context.
_PARTS = PATTERNS_GUIDE.split("\n### ")
_PREAMBLE: str = _PARTS[0]
_EXAMPLES: "dict[str, str]" = {c.split(" — ", 1)[0].strip(): "### " + c for c in _PARTS[1:]}


def guide_for(flags: "list[Flag]", kind: str, *, detail: bool = False, lists: bool = False) -> str:
    """The query-writing guide TRIMMED to what this page needs: the preamble (core syntax) + only
    the worked examples selected by the document ``kind`` and the situation -- a JSON/XML doc, an
    HTML list/table, a DETAIL-page follow (``detail``), a list-valued field (``lists``), and always
    the filtering example. Keeps the prompt lean vs. dumping all nine examples."""
    picks = {"json": ["3"], "xml": ["4"]}.get(kind, ["1", "2"])  # the record-shape example(s)
    if lists:
        picks.append("5")  # a list-valued field
    picks.append("7")  # filtering out rows (small, near-always useful)
    if detail:
        picks.append("8")  # a field on the DETAIL page (follow a link)
    if any(f.name == "record_list" and f.present for f in flags) and kind not in ("json", "xml"):
        picks.append("9")  # two sections / grouped selector -- when there are repeating records
    chosen: list[str] = []
    taken: set[str] = set()
    for p in picks:
        if p in _EXAMPLES and p not in taken:
            taken.add(p)
            chosen.append(_EXAMPLES[p])
    return _PREAMBLE.rstrip() + "\n\n" + "\n\n".join(chosen)


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
    return "PAGE SIGNALS (hardcoded detections about this page -- account for them):\n" + "\n".join(
        lines
    )


def field_schema(brief: DatasetBrief, *, selectors: bool = False) -> "list[str]":
    """The brief's fields as per-line schema entries -- ``  - name (type) -- description [selector]
    (optional)``. The single source both the Author prompt (:func:`fields_line`) and the Review
    prompt render from. ``selectors`` includes any explicit selector override (Author wants it; a
    plain schema for Review does not). ``(optional)`` marks a field that may legitimately be absent.
    """
    out: list[str] = []
    for name in brief.fields:
        piece = name
        if name in brief.types:
            piece += f" ({brief.types[name]})"
        if name in brief.descriptions:
            piece += f" -- {brief.descriptions[name]}"
        if selectors and name in brief.selectors:
            piece += f"  [selector: {brief.selectors[name]}]"
        if name in brief.optional:
            piece += "  (optional)"
        out.append("  - " + piece)
    return out


def fields_line(brief: DatasetBrief) -> str:
    """The requested fields as a labelled per-line schema for the AUTHOR prompt (selectors included)
    -- one field per line so a rich schema (type + description each) reads clearly."""
    if not brief.fields:
        return "Fields: (none given -- extract the salient fields of each record)."
    header = "Fields (each record should carry these -- name (type) -- description):\n"
    return header + "\n".join(field_schema(brief, selectors=True))


def _pager_note(flags: "list[Flag]") -> str:
    """The pagination note for an opening: the query covers ONE page; the pipeline pages."""
    present = [f.name for f in flags if f.present]
    return (
        "\nThe dataset spans multiple pages -- write the query for ONE page exactly as normal; "
        "the pipeline follows the pagination automatically. Do NOT add a 'next' field."
        if any(n in ("paginated", "infinite_scroll") for n in present)
        else ""
    )


def _notes(
    brief: DatasetBrief,
    flags: "list[Flag]",
    kind: str,
    *,
    record_selector: str = "",
    regions: str = "",
) -> str:
    """The advisory block every opening carries: the JSON-kind steer, the brief's structural
    guidance + requirement, the record selector LOCATE detected (a durable hook the model should
    prefer over a guessed class), and the page's fired flags (ground truth)."""
    notes: list[str] = []
    if record_selector:
        notes.append(
            f"DETECTED RECORD SELECTOR (from the page analysis): {record_selector} -- the repeating "
            "record region; prefer it (or a selector at least as durable) for the records."
        )
    if regions:
        notes.append(regions.strip())
    if kind == "json":
        notes.append(
            "This is a JSON document -- use dotted paths in select/select_all and read keys with "
            '.attr("<key>").'
        )
    if brief.author_hint:  # brief-specific STRUCTURAL guidance -- how this dataset is laid out
        notes.append(
            f"DATASET NOTES (from the brief -- how this dataset is laid out): {brief.author_hint}"
        )
    if brief.review_hint:  # the review criterion is a REQUIREMENT -- the author must know it
        notes.append(f"REQUIREMENT (the extracted data must satisfy this): {brief.review_hint}")
    if brief.expect_rows:  # a flexible guide for the record pick (never a hard rule)
        notes.append(
            f"EXPECTED SIZE (from the brief): about {brief.expect_rows} records per run -- a record "
            "selector that matches far more includes non-records; far fewer, the records are "
            "elsewhere (another section, a detail page, a feed)."
        )
    if brief.identity_hint:  # a SPECIFIC identity is asked for -> the author declares it
        notes.append(
            f"IDENTITY (from the brief): {brief.identity_hint} -- declare it explicitly with "
            '.identity(<field>, ...) on the records and/or .identity("<stable css>") on the '
            "detail page (identity is otherwise implicit: the hash of every extracted field)."
        )
    notes.append(_flags_line(flags))
    return "\n\n" + "\n\n".join(notes)


def author_prompt(
    brief: DatasetBrief,
    skeleton: str,
    flags: "list[Flag]",
    *,
    kind: str,
    detail: bool = False,
    recency: str = "",
    record_selector: str = "",
) -> str:
    """The OPENING turn that asks the model to write the extraction query -- rendered from the
    ``write_query`` prompt template (one prompt source, tunable as data): the signal-selected
    patterns guide, the ask (goal + per-line schema), the page's hardcoded signals/flags (ground
    truth the model must account for), the brief's structural guidance + requirement, and the page
    skeleton. ``kind`` is the document kind (``json`` steers to the dotted-path form); ``detail``
    adds the detail-page example; ``recency`` is the evaluator's read of the sort order + where the
    most recent records are. In a conversation this is sent ONCE; every repair is a short follow-up.
    """
    return render_prompt(
        "write_query",
        guide=guide_for(flags, kind, detail=detail),  # signal-selected examples, not all nine
        description=brief.goal or "the repeating dataset on this page",
        fields_line="\n" + fields_line(brief),
        pager=_pager_note(flags),
        skeleton=skeleton,
        hints=_notes(brief, flags, kind, record_selector=record_selector),
        recency=(f"\n\nRECENCY (from the page evaluation): {recency}" if recency else ""),
    )


#: the LEAF-READING part of the guide (reading a leaf / transforms / optional selects / durable
#: selectors) -- what the STEP engine needs from it: the whole-chain moves and the worked examples
#: are replaced by the op menu in the ``build_steps`` template.
LEAF_GUIDE: str = _PREAMBLE[_PREAMBLE.find("## Reading a leaf") :].rstrip()


def steps_prompt(
    brief: DatasetBrief,
    skeleton: str,
    flags: "list[Flag]",
    *,
    kind: str,
    recency: str = "",
    record_selector: str = "",
    regions: str = "",
) -> str:
    """The OPENING turn of the STEP-BY-STEP engine (``build_steps`` template): the leaf-reading
    guide, the ask, the op menu (records / field / detail / detail_field / where / drop / absent /
    section / done), the page's flags + notes (incl. the detected record selector), and the
    skeleton -- sent ONCE; every step is a short result turn."""
    return render_prompt(
        "build_steps",
        guide=LEAF_GUIDE,
        description=brief.goal or "the repeating dataset on this page",
        fields_line="\n" + fields_line(brief),
        pager=_pager_note(flags),
        skeleton=skeleton,
        hints=_notes(brief, flags, kind, record_selector=record_selector, regions=regions),
        recency=(f"\n\nRECENCY (from the page evaluation): {recency}" if recency else ""),
        start=(
            f'records("{record_selector}") -- the record list the page analysis detected.'
            if record_selector
            else 'records("<css>") -- the element marked ← RECORD LIST is the likely row.'
        ),
    )


def brief_hints(brief: DatasetBrief) -> str:
    """The brief's hints as one appended block for ANY stage prompt (search-verify / crawl / select /
    evaluate): the target schema (per line, typed + described -- the same :func:`field_schema` the
    author reads) plus the natural-language ``look`` / ``ignore`` guides. Empty when the brief
    carries none. ONE source, so every stage judges by the same schema and the same guides."""
    parts: list[str] = []
    if brief.fields:
        parts.append("Target schema (each record should carry):\n" + "\n".join(field_schema(brief)))
    if brief.look:
        parts.append("Head for pages like: " + "; ".join(brief.look) + ".")
    if brief.ignore:
        parts.append("Skip pages like: " + "; ".join(brief.ignore) + ".")
    return (" " + " ".join(parts)) if parts else ""


__all__ = [
    "PATTERNS_GUIDE",
    "LEAF_GUIDE",
    "author_prompt",
    "brief_hints",
    "guide_for",
    "steps_prompt",
]
