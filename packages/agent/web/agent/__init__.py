"""web.agent -- the agent tier: an observe->decide->apply loop that drives the lower layers.

This is where BoundedLoop belongs (its interrupt/resume is what an agent needs, unlike the simple
bounded loops below). The first agent is extraction AUTHORING: drive the loop to find the
selectors that pull rows from a page, with a pluggable driver (an LLM / heuristic / test stub).

    from web.agent import Author, Selection, Done
    result = await Author(doc, driver).run()   # result.rows, result.selection, result.verdict
"""

from __future__ import annotations

from .extract import Author, Authored, Done, Driver, Selection, extract
from .loop import Ask, BoundedLoop, Verdict

__all__ = ["BoundedLoop", "Verdict", "Ask", "Author", "Authored", "Selection", "Done", "Driver", "extract"]
