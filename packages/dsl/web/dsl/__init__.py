"""web.dsl -- the lazy execution engine on top: clean surfaces with clean joins.

Reference (drive) -> .doc() -> Document (read); Crawl (reach). Record a chain into a
serialisable Plan, then dispatch it in one mode from that ONE recording: sync (collect),
async (acollect), lazy (the surface itself), API/remote (to_blob + run_blob). The plain
layers below know nothing of this.

    from web.fetch import HttpFetcher
    from web.resolve import Resolver
    from web.dsl import DSL
    dsl = DSL(Resolver(HttpFetcher()))
    rows = dsl.ref("https://example.com").doc().select_all(".row").collect()   # sync
    rows = await dsl.ref(url).doc().select_all(".row").acollect()              # async
    blob = dsl.ref(url).doc().select_all(".row").to_blob()                     # API
    # actions return Self -> drive without snapshotting, join once with .doc():
    #   dsl.ref(url).click("#more").type("#q", "x").doc().select_all(".row")
"""

from __future__ import annotations

from .engine import DSL, Crawl, Document, Reference, run_blob
from .plan import Plan, Step

__all__ = ["DSL", "Reference", "Document", "Crawl", "run_blob", "Plan", "Step"]
