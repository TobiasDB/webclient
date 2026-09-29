"""web.agent -- the loop tier: the ONE bounded, interrupt/resumable control loop.

:class:`BoundedLoop` is the shared observe->decide->apply primitive every agentic loop derives
from -- its interrupt/resume (a ``decide`` may return an :class:`Ask` to hand off to a human, and
the loop is :meth:`~BoundedLoop.resume`d with the answer) is what sets it apart from the simple
bounded loops elsewhere. The authoring loop (``web.onboard.author_agent``) is its consumer.

    from web.onboard.agent import BoundedLoop, Ask, Done
"""

from __future__ import annotations

from .loop import Ask, BoundedLoop, Done, Verdict

__all__ = [
    "BoundedLoop",
    "Verdict",
    "Ask",
    "Done",
]
