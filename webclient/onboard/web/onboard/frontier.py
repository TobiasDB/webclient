"""An LLM-driven crawl frontier policy for Locate -- the webclient pipeline's ``_pick_edges``, as a
:data:`~web.crawl.FrontierMiddleware`. Each turn it hands the model the pending frontier and asks
which edges to expand next (best-first), so the crawl is goal-directed instead of blind
breadth-first. Optional: Locate's default is FIFO; pass this to make it LLM-driven. On any model
failure / unparseable reply it defers to the next handler (FIFO), so the crawl never breaks.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from web.crawl import FrontierItem, FrontierMiddleware, Select
from web.fetch import WebException, emit

from .llm import Llm, ReasonEvent

#: Cap how many frontier edges are shown to the model PER ROUND. The crawl frontier GROWS every round
#: (each fetched page adds all its anchors; only the picked few are removed), so sending the whole
#: pending list re-sends hundreds of URLs on EVERY round -- the crawl's runaway LLM token cost. A
#: keyword-ranked window of this size bounds the prompt regardless of how link-rich the site is.
_FRONTIER_WINDOW = 40
_WORD = re.compile(r"[a-z0-9]+")


def _tokens(*groups: "Sequence[str]") -> "set[str]":
    return {t for g in groups for s in g for t in _WORD.findall(s.lower()) if len(t) >= 3}


def _window(
    pending: "Sequence[FrontierItem]",
    goal: str,
    fields: "Sequence[str]",
    look: "Sequence[str]",
    ignore: "Sequence[str]",
) -> "list[FrontierItem]":
    """The most-promising slice of the frontier to show the model -- so the prompt stays bounded on a
    link-rich site. Ranks each edge by how many goal/field/look terms appear in its link text + URL
    (minus ignore terms), keeping the top :data:`_FRONTIER_WINDOW`. Below the cap it is a no-op (order
    preserved), so small crawls are unchanged."""
    if len(pending) <= _FRONTIER_WINDOW:
        return list(pending)
    want = _tokens([goal], fields, look)
    bad = _tokens(ignore)

    def score(i: int) -> "tuple[int, int]":
        it = pending[i]
        hay = set(_WORD.findall(f"{it.text} {it.url}".lower()))
        rel = sum(w in hay for w in want) - 2 * sum(w in hay for w in bad)
        return (-rel, i)  # highest relevance first; ties keep discovery order

    top = sorted(range(len(pending)), key=score)[:_FRONTIER_WINDOW]
    return [pending[i] for i in top]


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
    """The whole prompt (a stateless model): the standing instructions + this round's frontier."""
    return _opening(goal, _guides(goal, fields, look, ignore, entity)) + "\n\n" + _turn(pending, k)


def _guides(
    goal: str,
    fields: "Sequence[str]",
    look: "Sequence[str]",
    ignore: "Sequence[str]",
    entity: str = "",
) -> str:
    guides = ""
    if entity:
        guides += (
            f"\nThis dataset belongs to '{entity}'. ONLY expand links on {entity}'s OWN site (its "
            f"corporate domain, or its name-based investor-relations host, e.g. "
            f"{entity.split()[0].lower()}.q4cdn.com / {entity.split()[0].lower()}.gcs-web.com). "
            f"REJECT any third-party host that merely mentions {entity} — market-data aggregators "
            f"(Benzinga, MarketScreener, Yahoo Finance, Quartr, Seeking Alpha), news sites, stock "
            f"exchanges, brokerages: those are NOT the company's own data, never expand them."
        )
    if fields:
        guides += "\nEach record should have: " + ", ".join(fields)
    if look:
        guides += "\nPrefer links about: " + "; ".join(look)
    if ignore:
        guides += "\nAvoid links about: " + "; ".join(ignore)
    return guides


def _opening(goal: str, guides: str) -> str:
    """The crawl's standing instructions -- sent ONCE on a conversation, so every round's pick is a
    short turn over a cached prefix (the frontier calls were a locate's main LLM cost)."""
    return (
        f"You are crawling a website to find this dataset: {goal or 'the target dataset'}.{guides}\n\n"
        "Each round you get the links on the frontier (not yet fetched); each shows its link text "
        "and the status / title / detection flags of the page it was found on. Reply with ONLY a "
        'JSON array of the links to fetch next, most-promising first, each as {"n": <index>, '
        '"why": "<reason, at most 8 words>"} (e.g. [{"n": 3, "why": "the board listing"}]). Choose the links '
        "most likely to reach the dataset (a listing / records / a data API); omit nav, legal, "
        "login and unrelated sections. Do not re-pick a link you already chose in an earlier round."
    )


def _turn(pending: "Sequence[FrontierItem]", k: int) -> str:
    """One round's frontier + the pick budget."""
    listing = "\n".join(_edge(i, it) for i, it in enumerate(pending))
    return f"FRONTIER this round (pick at most {k}):\n{listing}\n\nJSON array only."


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

    async def ask(window: "Sequence[FrontierItem]") -> str:
        """The pick for this round -- ONE-SHOT on purpose: a conversation would re-read every
        earlier round's listing on each turn (measured: the per-call cost climbed with the context
        while the replies shrank), and a round needs only its own frontier. The standing opening
        is short; the listing window is bounded."""
        return await llm.complete(_prompt(goal, fields, look, ignore, window, k, entity))

    async def mw(pending: "Sequence[FrontierItem]", nxt: Select) -> "Sequence[FrontierItem]":
        if len(pending) <= 1:
            return await nxt(pending)  # nothing to choose
        # show the model only a BOUNDED, keyword-ranked window of the frontier -- the pending list
        # grows every round, so sending all of it is the crawl's runaway token cost.
        window = _window(pending, goal, fields, look, ignore)
        try:
            reply = await ask(window)
        except WebException as exc:  # model unavailable -> plain breadth-first, SAY WHY
            emit(
                ReasonEvent(
                    stage="frontier",
                    text=f"model error ({exc.error.code}: {exc.error.message}) — this round falls "
                    "back to breadth-first",
                )
            )
            return await nxt(pending)
        picks: list[FrontierItem] = []
        for idx, why in _picks(reply, len(window))[:k]:
            picks.append(window[idx])
            emit(
                ReasonEvent(stage="frontier", subject=window[idx].url, text=why)
            )  # WHY it was picked
        return picks or await nxt(pending)

    return mw


__all__ = ["llm_frontier"]
