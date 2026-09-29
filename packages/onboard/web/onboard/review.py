"""The Author REVIEW step: run the authored query, show the model its sample rows against the
schema, and let it REVISE the query -- fix selectors, add missing fields, follow a link for detail
(e.g. the full body, not a listing summary) -- up to N rounds. Optional; a caller opts in
(``web author --review N``). It NEVER makes things worse: a revision that does not parse, or a round
the model says is DONE, keeps the current query; a run failure ends the loop.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import cast

from web.fetch import WebException, emit

from web.resolve import Resolver

from .author import AuthorEvent
from .compile import Query, QueryError, parse_query, reroot
from .llm import Llm, ReasonEvent
from .models import DatasetBrief, Reference

#: replies that mean "the query is already good" (no wq chain, an affirmative).
_DONE = ("done", "no change", "looks good", "correct", "complete", "ok")


def _schema_lines(brief: DatasetBrief) -> str:
    if not brief.fields:
        return "(no explicit schema -- the salient fields of each record, in full detail)"
    out: list[str] = []
    for f in brief.fields:
        piece = f + (f" ({brief.types[f]})" if f in brief.types else "")
        if f in brief.descriptions:
            piece += f" -- {brief.descriptions[f]}"
        if f in brief.optional:
            piece += " (optional)"
        out.append("  - " + piece)
    return "\n".join(out)


def _review_prompt(brief: DatasetBrief, query: str, rows: "Sequence[object]") -> str:
    sample = json.dumps(rows[:5], ensure_ascii=False, indent=2, default=str)
    return (
        f"You wrote this wq query to extract '{brief.goal or 'the dataset'}':\n{query}\n\n"
        f"It produced these sample rows:\n{sample}\n\n"
        f"The schema wants each record to carry:\n{_schema_lines(brief)}\n\n"
        "Review the rows against the schema. Is every requested field present and correctly "
        "extracted, in the DETAIL asked for (e.g. the full body text, not a listing summary; a "
        "clean value, not surrounding markup)? Are the selectors right? Is a field empty, wrong, "
        "or missing?\n\n"
        "If the query already extracts the schema well, reply with exactly: DONE\n"
        "Otherwise reply with ONLY a REVISED wq.doc... query that fixes it -- better selectors, the "
        "missing fields, or following a record's link to get detail (resolve the link and select on "
        "the detail page). No prose, no code fence."
    )


def _is_done(reply: str) -> bool:
    text = reply.strip().lower()
    return "wq." not in reply and any(d in text[:40] for d in _DONE)


async def _sample(query: Query, resolver: Resolver, n: int = 5) -> "list[object]":
    result: object = await query.acollect(resolver=resolver)
    if isinstance(result, list):
        return cast("list[object]", result[:n])
    return [result]


async def review(
    query: Query,
    reference: Reference,
    brief: DatasetBrief,
    *,
    resolver: Resolver,
    llm: Llm,
    rounds: int = 1,
) -> "tuple[Query, list[str]]":
    """Run the query, show the model its rows vs the schema, and REVISE it up to ``rounds`` times.
    Returns the (possibly improved) query + the review notes. Falls back to the current query on any
    parse/run failure or a ``DONE`` reply, so review never regresses the result."""
    notes: list[str] = []
    for r in range(1, rounds + 1):
        try:
            rows = await _sample(query, resolver)
        except WebException as exc:
            emit(ReasonEvent(stage="review", text=f"could not run the query to review it ({exc})"))
            break
        reply = await llm.complete(_review_prompt(brief, query.describe(), rows))
        if _is_done(reply):
            emit(ReasonEvent(stage="review", text=f"round {r}: the data satisfies the schema"))
            break
        try:
            revised = reroot(parse_query(reply), reference.url, profile=reference.profile or None)
        except QueryError:
            emit(
                ReasonEvent(
                    stage="review",
                    text=f"round {r}: proposed revision did not parse -- kept the query",
                )
            )
            break
        emit(AuthorEvent(phase="reply", reply=reply))
        emit(ReasonEvent(stage="review", subject=f"round {r}", text="revised the query"))
        query = revised
        notes.append(f"review round {r}: query revised")
    return query, notes


__all__ = ["review"]
