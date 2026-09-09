"""webclient — references, documents, and one expression language over both.

    from webclient import WebClient, doc, el, field

    with WebClient() as wc:
        page = wc.resolve("https://example.com")
        page.select("h1").attr("text")

        page.then(rows=doc.select_all(".card").map(
            el.select("h3").attr("text").alias("title"),
            price=el.select(".price").attr("text"),
        ))

`then` / `map` / `otherwise` are methods on the real classes; `doc`, `el`,
`ref` and `err` are those same classes with recording switched on. One
implementation serves both, so a plan cannot mean something different from
the code that would compute it directly.
"""
from .backing import (Backing, HttpBacking, Kind, StaticBacking, TreeBacking,
                      charset_of, sniff_kind)
from .document import Document, Element
from .errors import (PlanError, ResolveError, SelectionError, StaleDocument,
                     UnsupportedOperation, WebClientError)
from .ops import CoreView, OpSpec, REGISTRY, op, op_property
from .plan import Plan, Projection, Source
from .records import Err, Record, RecordSet
from .reference import HttpMethod, Proxy, Reference, Script
from .render import Block, RendererRegistry, default_registry
from .roots import DROP_ROW, NULL, RAISE_ERROR, doc, el, err, field, lit, ref
from .telemetry import (ConsoleRecord, Observers, RedirectRecord,
                        RequestRecord, RetryRecord, Telemetry)
from .values import Expr, Failure, Selection, Value

from .client import WebClient, default_client
from .pool import ClientPool, Lease, PoolStats
from .session import Session

__all__ = [
    # core
    "WebClient", "default_client", "Document", "Element", "Reference",
    "Session",
    # expression language
    "doc", "el", "ref", "err", "field", "lit",
    "RAISE_ERROR", "DROP_ROW", "NULL",
    "Expr", "Value", "Selection", "Record", "RecordSet", "Err", "Failure",
    "Plan", "Projection", "Source", "PlanError",
    # backings
    "Backing", "TreeBacking", "StaticBacking", "HttpBacking", "Kind",
    "sniff_kind", "charset_of",
    # rendering
    "Block", "RendererRegistry", "default_registry",
    # telemetry
    "Telemetry", "Observers", "RequestRecord", "RedirectRecord",
    "ConsoleRecord", "RetryRecord",
    # extension
    "op", "op_property", "OpSpec", "REGISTRY", "CoreView",
    "ClientPool", "Lease", "PoolStats",
    # errors
    "WebClientError", "UnsupportedOperation", "StaleDocument", "ResolveError",
    "SelectionError",
]
