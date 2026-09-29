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
from web.fetch import WebException

from .llm import Llm


def _indices(reply: str, n: int) -> "list[int]":
    """The distinct in-range indices in a model's JSON-array reply (``[3, 0, 7]``); ``[]`` if none."""
    start, end = reply.find("["), reply.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        data: object = json.loads(reply[start : end + 1])
    except ValueError:
        return []
    out: list[int] = []
    for x in data if isinstance(data, list) else []:
        if isinstance(x, int) and 0 <= x < n and x not in out:
            out.append(x)
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
) -> str:
    guides = ""
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
        f"Reply with ONLY a JSON array of the indices to fetch next, most-promising first, at most "
        f"{k} (e.g. [3, 0, 7]). Choose the links most likely to reach the dataset (a listing / "
        f"records / a data API); omit nav, legal, login and unrelated sections."
    )


def llm_frontier(
    llm: Llm,
    goal: str = "",
    *,
    fields: "Sequence[str]" = (),
    look: "Sequence[str]" = (),
    ignore: "Sequence[str]" = (),
    k: int = 5,
) -> FrontierMiddleware:
    """A frontier middleware that asks ``llm`` which pending edges to expand next (best-first, at
    most ``k``), guided by the ``goal`` + ``fields`` (the schema) + the brief's ``look`` / ``ignore``,
    and each edge's link text + the status/title/flags of the page it was found on. Falls back to
    the next handler (FIFO) when the model errors, replies unparseably, or picks nothing."""

    async def mw(pending: "Sequence[FrontierItem]", nxt: Select) -> "Sequence[FrontierItem]":
        if len(pending) <= 1:
            return await nxt(pending)  # nothing to choose
        try:
            reply = await llm.complete(_prompt(goal, fields, look, ignore, pending, k))
        except WebException:
            return await nxt(pending)  # model unavailable -> plain breadth-first, don't break
        picks = [pending[i] for i in _indices(reply, len(pending))[:k]]
        return picks or await nxt(pending)

    return mw


__all__ = ["llm_frontier"]
