"""An LLM-backed :class:`~web.agent.extract.Driver` -- the intelligence for extraction authoring.

It turns a :class:`~web.llm.Llm` into a driver: each round it prompts the model with the goal, the
page, and the rows the last :class:`Selection` produced, and parses the reply into the next
Selection (or ``Done``). The loop (which now awaits an async ``decide``) drives it. This is the one
place LLM ↔ agent meet; the agent stays testable with a plain stub driver, and the LLM client
stays ignorant of pages and selectors.
"""

from __future__ import annotations

import json
import re

from web.llm import Llm
from web.parse import Document

from .extract import Done, Driver, Selection

_PROMPT = """You extract repeating structured rows from a web page.

Goal: {goal}

Reply with ONLY a JSON object, either:
  {{"row": "<CSS selector for one repeating row>", "fields": {{"<name>": "<CSS relative to the row>"}}}}
or, if the rows already extracted below satisfy the goal:
  {{"done": true}}

Rows extracted so far: {rows}

Page HTML (truncated):
{html}
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
    return Selection(row=str(obj["row"]), fields={str(k): str(v) for k, v in obj.get("fields", {}).items()})


def llm_driver(llm: Llm, goal: str, *, page_chars: int = 6000) -> Driver:
    """A Driver that asks ``llm`` for the next :class:`Selection` given the page + last rows."""

    async def driver(doc: Document, rows: "list[dict[str, str | None]]") -> "Selection | Done":
        prompt = _PROMPT.format(goal=goal, rows=rows, html=doc.text[:page_chars])
        return _parse(await llm.complete(prompt))

    return driver


__all__ = ["llm_driver"]
