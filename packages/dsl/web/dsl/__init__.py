"""web.dsl -- the lazy execution engine on top of the plain layers.

Record a chain of method calls into a serialisable Plan, then dispatch it in one of four modes
from that ONE recording: sync (collect), async (acollect), lazy (the Lazy/Plan itself), and
API/remote (to_blob + run_blob on a server). The plain layers below know nothing of this -- the
DSL wraps their ordinary methods and provides all the laziness, blocking, and remoting.

    from web.fetch import HttpFetcher
    from web.resolve import Resolver
    from web.dsl import DSL
    dsl = DSL(Resolver(HttpFetcher()))
    title = dsl.get("https://example.com").select("title")   # lazy: nothing ran yet
    els = title.collect()                                     # sync dispatch
    els = await title.acollect()                              # async dispatch
    blob = title.to_blob()                                    # API dispatch: ship the plan
"""

from __future__ import annotations

from .engine import DSL, Lazy, run_blob
from .plan import Plan, Step

__all__ = ["DSL", "Lazy", "run_blob", "Plan", "Step"]
