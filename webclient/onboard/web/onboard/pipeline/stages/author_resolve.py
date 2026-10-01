"""Stage 7 -- author resolve: the request that returns the dataset's document. Deterministic:
the data API when the source has one (fetched at the cheap tier), else the page at the tier the
review needed. (Pagination / several source URLs join this stage later.)"""

from __future__ import annotations

from web.fetch import emit

from ...llm import ReasonEvent
from ..ask import Context
from ..state import Onboarding, ResolvePlan


async def run(state: Onboarding, ctx: Context) -> ResolvePlan:
    assert state.expand is not None
    src = state.expand
    if src.api is not None and src.api.usable:  # a replayable GET feed; else the page
        url, profile, via = src.api.url, "basic", True
    else:
        url, profile, via = (
            src.url,
            (src.spa.profile if src.spa is not None else src.profile),
            False,
        )
    emit(
        ReasonEvent(
            stage="author_resolve",
            subject=url,
            text=f"{'the data api' if via else 'the page'} at {profile}",
        )
    )
    return ResolvePlan(
        url=url,
        profile=profile,
        via_api=via,
        source=f"wq.reference({url!r}).resolve(profile={profile!r})",
    )
