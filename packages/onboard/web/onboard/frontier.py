"""An LLM-driven crawl frontier policy for Locate -- the webclient pipeline's ``_pick_edges``, as a
:data:`~web.crawl.FrontierMiddleware`. Each turn it hands the model the pending frontier and asks
which edges to expand next (best-first), so the crawl is goal-directed instead of blind
breadth-first. Optional: Locate's default is FIFO; pass this to make it LLM-driven. On any model
failure / unparseable reply it defers to the next handler (FIFO), so the crawl never breaks.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from web.crawl import FrontierItem, FrontierMiddleware, Select
from web.fetch import WebException, emit

from .llm import Llm, ReasonEvent


def _picks(reply: str, n: int) -> "list[tuple[int, str]]":
    """The reasoned picks in a model reply: a JSON array of ``{"n": <index>, "why": "<reason>"}``
    objects (or bare indices for robustness) -> ``[(index, why), ...]``, in range and deduped."""
    start, end = reply.find("["), reply.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        data: object = json.loads(reply[start : end + 1])
    except ValueError:
        return []
    out: list[tuple[int, str]] = []
    seen: set[int] = set()
    for item in data if isinstance(data, list) else []:
        if isinstance(item, dict):
            idx = item.get("n", item.get("index"))
            why = str(item.get("why") or item.get("reason") or "")
        else:
            idx, why = item, ""
        if isinstance(idx, int) and 0 <= idx < n and idx not in seen:
            seen.add(idx)
            out.append((idx, why))
    return out


def _edge(i: int, it: "FrontierItem") -> str:
    """One frontier line: the link (text) + the assessment of the page it was found on -- the
    parent's status, title and the detection flags that fired there (record_list / data_api / ...),
    so the model routes with real context, not URL shape alone."""
    line = f"{i}. {it.url}"
    if it.text:
        line += f'   "{it.text[:80]}"'
    ctx = []
    if it.parent_status:
        ctx.append(f"status {it.parent_status}")
    if it.parent_title:
        ctx.append(it.parent_title[:60])
    if it.parent_flags:
        ctx.append("flags: " + ",".join(it.parent_flags))
    if ctx:
        line += f"   (found on {it.parent} — {' · '.join(ctx)})"
    return line


def _prompt(
    goal: str,
    fields: "Sequence[str]",
    look: "Sequence[str]",
    ignore: "Sequence[str]",
    pending: "Sequence[FrontierItem]",
    k: int,
    entity: str = "",
) -> str:
    guides = ""
    if entity:
        guides += (
            f"\nThis dataset belongs to '{entity}'. ONLY expand links on {entity}'s OWN site (its "
            f"domain, or its name-based investor-relations host, e.g. {entity.split()[0].lower()}."
            f"q4cdn.com). REJECT third-party sources — news, market-data aggregators, exchanges, "
            f"brokerages — that merely mention it; those are NOT the company's own data."
        )
    if fields:
        guides += "\nEach record should have: " + ", ".join(fields)
    if look:
        guides += "\nPrefer links about: " + "; ".join(look)
    if ignore:
        guides += "\nAvoid links about: " + "; ".join(ignore)
    listing = "\n".join(_edge(i, it) for i, it in enumerate(pending))
    return (
        f"You are crawling a website to find this dataset: {goal or 'the target dataset'}.{guides}\n\n"
        f"These links are on the frontier (not yet fetched). Each shows its link text and the "
        f"status / title / detection flags of the page it was found on:\n{listing}\n\n"
        f"Reply with ONLY a JSON array of the links to fetch next, most-promising first, at most "
        f'{k}, each as {{"n": <index>, "why": "<short reason>"}} (e.g. '
        f'[{{"n": 3, "why": "the board listing"}}]). Choose the links most likely to reach the '
        f"dataset (a listing / records / a data API); omit nav, legal, login and unrelated sections."
    )


def llm_frontier(
    llm: Llm,
    goal: str = "",
    *,
    entity: str = "",
    fields: "Sequence[str]" = (),
    look: "Sequence[str]" = (),
    ignore: "Sequence[str]" = (),
    k: int = 5,
) -> FrontierMiddleware:
    """A frontier middleware that asks ``llm`` which pending edges to expand next (best-first, at
    most ``k``), guided by the ``goal`` + ``fields`` (the schema) + the brief's ``look`` / ``ignore``,
    and each edge's link text + the status/title/flags of the page it was found on. When ``entity``
    is set (a company/site the dataset belongs to) the model is told to expand ONLY the entity's own
    pages and reject third-party sources that merely mention it. Falls back to the next handler
    (FIFO) when the model errors, replies unparseably, or picks nothing."""

    async def mw(pending: "Sequence[FrontierItem]", nxt: Select) -> "Sequence[FrontierItem]":
        if len(pending) <= 1:
            return await nxt(pending)  # nothing to choose
        try:
            reply = await llm.complete(_prompt(goal, fields, look, ignore, pending, k, entity))
        except WebException:
            return await nxt(pending)  # model unavailable -> plain breadth-first, don't break
        picks: list[FrontierItem] = []
        for idx, why in _picks(reply, len(pending))[:k]:
            picks.append(pending[idx])
            emit(
                ReasonEvent(stage="frontier", subject=pending[idx].url, text=why)
            )  # WHY it was picked
        return picks or await nxt(pending)

    return mw


__all__ = ["llm_frontier"]
