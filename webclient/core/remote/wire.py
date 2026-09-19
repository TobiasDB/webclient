"""Remote wire machinery: the (de)serialisation + guards the remote dispatch shares.

Separated from the client core so it can back a thin remote transport: the client just
POSTs a plan and hands the JSON here to rebuild real cores -- a ``Document`` handle, a
``Reference``, or a value model -- and these guards decide what may cross the wire. Kept
free of any client subclass so remote stays "a dispatch mode + this wire", not a bespoke
``WebClient`` subtype.
"""

from __future__ import annotations

from typing import Any, cast

from ..document import Document
from ..reference import Reference


def url_of(source: dict[str, Any]) -> str:
    """The URL a reference-rooted plan's ``source`` spec resolves to."""
    return cast(str, Reference(**source).dispatch("url"))


def reject_sequence(expr: Any) -> None:
    """A ``.step(...)`` sequence holds ONE live page across ordered actions. A COMPLETE
    stepful plan -- one that ends in ``.project()`` -- produces DATA: its held page lives
    entirely inside a single server-side ``/execute`` evaluation and never crosses the
    wire, so it runs remotely like any other data-producing browser plan. An OPEN stepful
    plan (one that would hand back a live page/element -- e.g. it ends in
    ``select``/``select_all``) has no remote representation for that held page, so it
    stays engine-local: fail clearly, and tell the caller to close it with ``.project()``."""
    plan = getattr(expr, "_plan", None)
    gets = [s for s in (getattr(plan, "steps", None) or ()) if s.kind == "get"]
    if not any(s.name == "step" for s in gets):
        return  # no sequence -- nothing to guard
    if gets and gets[-1].name == "project":
        return  # a complete, data-producing sequence -- safe to run server-side
    raise NotImplementedError(
        "an OPEN .step(...) sequence (one that returns a live page/element) runs only on "
        "a local client; end it with .project() to produce data and run it remotely"
    )


_WIRE_MODELS_CACHE: "dict[str, type[Any]] | None" = None


def wire_models() -> "dict[str, type[Any]]":
    """Name -> class for the value models an op can return over the wire (built once), so
    :func:`deserialize` rebuilds a real ``Transport``/``Metadata``/… model from a tagged
    ``{"__model__": ...}`` payload."""
    global _WIRE_MODELS_CACHE
    if _WIRE_MODELS_CACHE is None:
        from ...models import (
            ActionEvent,
            ConsoleEvent,
            DOMUpdateEvent,
            Event,
            NavigationEvent,
            NetworkEvent,
            PlanEvent,
        )
        from ..client.models import Robots
        from ..crawl.models import Edge
        from ..document.models import (
            Element,
            Flag,
            Metadata,
            PageCard,
            Signal,
            Structure,
            Transport,
        )

        models: list[type[Any]] = [
            Transport, Metadata, Structure, Signal, Flag, Element, PageCard,
            Edge, Robots, Event, NavigationEvent, NetworkEvent, ConsoleEvent,
            DOMUpdateEvent, ActionEvent, PlanEvent,
        ]
        _WIRE_MODELS_CACHE = {m.__name__: m for m in models}
    return _WIRE_MODELS_CACHE


def doc_handle(client: Any, meta: dict[str, Any]) -> Document:
    """A server-side document as a real ``Document`` bound to ``client``: id/kind/ok are
    inline (``status_code`` set so ``ok`` agrees); content ops round-trip (``_remote_handle``)."""
    doc = Document(
        url="",
        kind=meta.get("kind", "html"),
        status_code=200 if meta.get("ok", True) else 502,
    )
    doc.id = doc.name = meta["id"]
    doc._client = client
    doc._remote_handle = True
    return doc


def deserialize(client: Any, rows: Any) -> Any:
    """Rebuild real cores/values from the service's clean JSON, bound to ``client`` -- a
    ``{"__doc__"}`` handle, a ``{"__ref__"}`` reference, a ``{"__model__"}`` value model,
    or a list thereof; anything else passes through."""
    if isinstance(rows, dict) and "__doc__" in rows:
        return doc_handle(client, rows["__doc__"])
    if isinstance(rows, dict) and "__ref__" in rows:
        ref = Reference(**rows["__ref__"])
        ref._client = client
        return ref
    if isinstance(rows, dict) and "__model__" in rows:
        # rebuild the real value model (Transport/Metadata/…) so a remote result has the
        # same type as a local one (s.title, not s["title"]).
        model = wire_models().get(rows["__model__"])
        data = rows.get("data", {})
        return model.model_validate(data) if model is not None else data
    if isinstance(rows, list):
        return [deserialize(client, r) for r in rows]
    return rows


__all__ = ["url_of", "reject_sequence", "wire_models", "doc_handle", "deserialize"]
