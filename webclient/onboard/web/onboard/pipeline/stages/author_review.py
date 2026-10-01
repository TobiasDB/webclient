"""Stage author_review -- TODO (built in the next slice)."""

from __future__ import annotations

from pydantic import BaseModel

from ..ask import Context
from ..state import Onboarding


async def run(state: Onboarding, ctx: Context) -> BaseModel:
    raise NotImplementedError("stage author_review is not built yet")
