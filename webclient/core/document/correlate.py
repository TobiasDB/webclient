"""XHR -> DOM correlation: which network request produced which DOM region.

Deliberately SILOED behind the :class:`Correlator` protocol so the cheap, ordering-based
attribution here can be swapped for -- or refined by -- a content-matching one without
touching the skeleton or any caller.

Attribution is INFERENCE, never proof. A DOM update can only follow the request that
caused it (a HARD temporal fact -- the lower bound this baseline exploits), but *which*
of several near-simultaneous completed requests is responsible is a judgement we SCORE,
not decide. The baseline keeps every temporally-possible request as a candidate; a richer
:class:`Correlator` (e.g. entropy-weighted value matching against response bodies) narrows
that residue behind the same interface.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from ...models import DOMUpdateEvent, NetworkEvent


class XhrRequest(BaseModel):
    """One correlated network request, in completion order."""

    index: int  # 1-based COMPLETION order among xhr/fetch (the phase the DOM stamps key to)
    method: str = "GET"
    url: str = ""
    t_s: float = 0.0  # seconds since the FIRST request (relative float)


class DomPhase(BaseModel):
    """The request/action that could have produced a DOM node's content."""

    node_key: str  # the set-once ``data-wc-node`` id of the mutated node
    candidates: list[int] = []  # XhrRequest.index values; EMPTY = pre-XHR (server-static or pure JS)
    action: int | None = None  # the ACTION index that first revealed the node (a .click/.write);
    # None = it appeared without an action (server-static / load-time JS / an XHR)
    t_s: float | None = None  # when the node last mutated (seconds since the first request)


class Correlation(BaseModel):
    """The full attribution: the request timeline + the per-node candidate requests.
    Consumed by the skeleton (to annotate ``<- after [n]``) without knowing which
    :class:`Correlator` produced it."""

    requests: list[XhrRequest] = []
    phases: list[DomPhase] = []

    def candidates_for(self, node_key: str) -> list[int]:
        """The candidate request indices for a node (``[]`` if unknown or pre-XHR)."""
        for p in self.phases:
            if p.node_key == node_key:
                return p.candidates
        return []

    def action_for(self, node_key: str) -> "int | None":
        """The action index that first revealed a node (``None`` if none / not action-driven)."""
        for p in self.phases:
            if p.node_key == node_key:
                return p.action
        return None

    def request(self, index: int) -> "XhrRequest | None":
        for r in self.requests:
            if r.index == index:
                return r
        return None


@runtime_checkable
class Correlator(Protocol):
    """Attribute DOM regions to the network requests that likely produced them.
    THE stable seam: an expensive content-matching implementation is a drop-in for
    the cheap :class:`OrderingCorrelator` -- same input, same :class:`Correlation`."""

    def correlate(
        self, network: "list[NetworkEvent]", dom: "list[DOMUpdateEvent]"
    ) -> Correlation: ...


def _request_url(ev: NetworkEvent) -> str:
    req = ev.request
    if req is None:
        return ""
    try:
        return str(req.dispatch("url"))
    except Exception:  # noqa: BLE001 - a bare reference / odd shape: no url is fine here
        return ""


def _requests(network: "list[NetworkEvent]") -> "list[XhrRequest]":
    """The correlated requests (those carrying a completion ``index``), in order."""
    out: list[XhrRequest] = []
    for ev in network:
        if ev.index is None:
            continue
        out.append(
            XhrRequest(
                index=int(ev.index),
                method=(ev.method or "GET").upper(),
                url=_request_url(ev),
                t_s=float(ev.t_s or 0.0),
            )
        )
    out.sort(key=lambda r: r.index)
    return out


class OrderingCorrelator:
    """The cheap, robust baseline -- pure temporal ordering, no response bodies.

    A node's candidates are the request(s) completed just before its mutation: the
    latest-completed request (a hard lower bound) plus any earlier request that completed
    within ``window_s`` of it (near-simultaneous, so genuinely ambiguous). Ambiguity is
    PRESERVED as a candidate list, never falsely resolved -- that is a richer correlator's
    job. Cost is O(events)."""

    def __init__(self, window_s: float = 0.15) -> None:
        self.window_s = window_s

    def correlate(
        self, network: "list[NetworkEvent]", dom: "list[DOMUpdateEvent]"
    ) -> Correlation:
        requests = _requests(network)
        by_index = {r.index: r for r in requests}
        phases: dict[str, DomPhase] = {}
        for ev in dom:
            node = ev.node_id or ""
            if not node:  # a mutation we cannot tie to a stable node -- skip (no false phase)
                continue
            detail: dict[str, Any] = ev.detail or {}
            xhr_index = int(detail.get("xhr_index", 0) or 0)
            action = int(detail.get("action", 0) or 0)
            t_s = detail.get("t_s")
            cands = self._candidates(xhr_index, requests, by_index)
            existing = phases.get(node)
            if existing is None:
                # the FIRST stamp for a node is when it appeared -> its revealing action.
                phases[node] = DomPhase(
                    node_key=node, candidates=cands, action=action or None, t_s=t_s
                )
            else:  # a node mutated more than once -- merge every xhr phase it passed through
                existing.candidates = sorted(set(existing.candidates) | set(cands))
                if existing.action is None and action:  # keep the earliest (appearance) action
                    existing.action = action
                if t_s is not None:
                    existing.t_s = t_s if existing.t_s is None else max(existing.t_s, t_s)
        return Correlation(requests=requests, phases=list(phases.values()))

    def _candidates(
        self, xhr_index: int, requests: "list[XhrRequest]", by_index: "dict[int, XhrRequest]"
    ) -> "list[int]":
        if xhr_index <= 0:  # mutated before any request completed -> server-static or pure JS
            return []
        latest = by_index.get(xhr_index)
        if latest is None:  # a stamp with no matching request record -- keep it, honestly
            return [xhr_index]
        out = {xhr_index}
        for r in requests:  # cluster near-simultaneous earlier completions as co-candidates
            if r.index < xhr_index and (latest.t_s - r.t_s) <= self.window_s:
                out.add(r.index)
        return sorted(out)


__all__ = ["XhrRequest", "DomPhase", "Correlation", "Correlator", "OrderingCorrelator"]
