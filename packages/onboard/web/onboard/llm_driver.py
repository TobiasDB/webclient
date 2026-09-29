"""An LLM-backed :class:`~web.onboard.agent.extract.Driver` -- the intelligence for extraction authoring.

It turns a :class:`.llm.Llm` into a driver: each round it prompts the model with the goal, the
page, and the rows the last :class:`Selection` produced, and parses the reply into the next
Selection (or ``Done``). The loop (which now awaits an async ``decide``) drives it. This is the one
place LLM ↔ agent meet; the agent stays testable with a plain stub driver, and the LLM client
stays ignorant of pages and selectors.
"""

from __future__ import annotations

import json
import re

from web.parse import Document

from .agent import Done, Driver, Selection
from .llm import Llm

_PROMPT = """You extract repeating structured rows from a web page.

Goal: {goal}

Reply with ONLY a JSON object, either:
  {{"row": "<CSS selector for one repeating row>", "fields": {{"<name>": "<CSS relative to the row>"}}}}
or, if the rows already extracted below satisfy the goal:
  {{"done": true}}

Detected record region(s) -- a good starting point for "row": {records}
Rows extracted so far: {rows}

Page skeleton (a token-lean DOM outline; a "RECORD LIST" mark shows a likely row selector):
{skeleton}
"""


def _parse(reply: str) -> "Selection | Done":
    """Pull the JSON object out of the reply and turn it into a decision; an unparseable reply
    stops the loop (``Done``) rather than crashing it."""
    m = re.search(r"\{.*\}", reply, re.S)
    if m is None:
        return Done()
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return Done()
    if obj.get("done") or "row" not in obj:
        return Done()
    return Selection(
        row=str(obj["row"]),
        fields={str(k): str(v) for k, v in obj.get("fields", {}).items()},
    )


def llm_driver(llm: Llm, goal: str, *, skeleton_lines: int = 200) -> Driver:
    """A Driver that asks ``llm`` for the next :class:`Selection`. It sends the token-lean, record-
    marked skeleton (not raw HTML) plus the detected record selectors -- cheaper and far more
    selector-authorable than truncated source."""

    async def driver(
        doc: Document, rows: "list[dict[str, str | None]]"
    ) -> "Selection | Done":
        records = (
            "; ".join(
                f'select_all("{r.item_selector}") ({r.count} items)'
                for r in doc.records(top_k=3)
            )
            or "(none detected)"
        )
        prompt = _PROMPT.format(
            goal=goal,
            records=records,
            rows=rows,
            skeleton=doc.skeleton(max_lines=skeleton_lines),
        )
        return _parse(await llm.complete(prompt))

    return driver


__all__ = ["llm_driver"]
