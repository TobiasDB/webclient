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

import json
import math
import re
from collections import Counter
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
    confidence: float | None = None  # OPTIONAL: set by a content-matching Correlator when it
    # NARROWS an ambiguous ordering result -- 0..1, the share of the winning candidate's evidence
    # (None = never narrowed by content; the ordering baseline leaves it unset)


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


# --------------------------------------------------------------------------- #
# ContentCorrelator: refine the ordering baseline by response-body value matching
# --------------------------------------------------------------------------- #

#: closed-class / very common words that are worthless as discriminators (they appear in
#: almost any body and any DOM text). Kept small -- specificity scoring already ~zeroes
#: short common words; this just spares a handful of common longer stopwords.
_STOP = frozenset(
    "the and for are but not you all any can had her was one our out day get has him his how man "
    "new now old see two way who boy did its let put say she too use with from this that they them "
    "then have will your what when your about which their there where would these than been more "
    "http https www com org net html json data true false null value items item list page name "
    "type text title href link node".split()
)

#: alphanumeric word runs, and longer "value" runs that keep the structural punctuation of a
#: GUID / ISO-date / price / slug / path (so ``550e8400-e29b-41d4`` stays ONE token, not three).
_WORD = re.compile(r"[a-z0-9]+")
_VALUE = re.compile(r"[a-z0-9][a-z0-9._:/@%-]{3,}")


def _text_tokens(s: str) -> "set[str]":
    """Tokenise a string the SAME way for a node's text and a body, so a value that appears
    in both matches. Case-folded; both bare words and punctuation-carrying value runs."""
    s = s.lower()
    return set(_WORD.findall(s)) | set(_VALUE.findall(s))


def _json_leaves(data: Any, out: "list[str]", budget: int = 20000) -> None:
    """Every scalar leaf (string / number / bool) of a parsed JSON value, as strings -- the
    values a rendered node's text is most likely to echo. Bounded so a huge blob can't blow up."""
    if len(out) >= budget:
        return
    if isinstance(data, dict):
        for v in data.values():
            _json_leaves(v, out, budget)
    elif isinstance(data, list):
        for v in data:
            _json_leaves(v, out, budget)
    elif isinstance(data, bool):
        out.append("true" if data else "false")
    elif isinstance(data, (str, int, float)):
        out.append(str(data))


def _body_tokens(body: "bytes | None") -> "set[str]":
    """The token set of a response body: JSON leaf strings/numbers when it parses as JSON,
    plus generic text tokens over the whole payload (covers non-JSON bodies and the inner
    words of JSON string values). Empty for a missing body."""
    if not body:
        return set()
    text = body.decode("utf-8", "replace")
    toks: set[str] = set()
    try:
        leaves: list[str] = []
        _json_leaves(json.loads(text), leaves)
        for leaf in leaves:
            toks |= _text_tokens(leaf)
    except (ValueError, RecursionError):
        pass  # not JSON, or pathologically nested -- the generic pass below still tokenises it
    toks |= _text_tokens(text)
    return toks


def _specificity(tok: str) -> float:
    """How DISCRIMINATING a token is -- rarity/length/entropy. A short common word ≈ 0; a long
    high-entropy value (GUID / price / ISO-date / long unique slug) scores high. Character
    Shannon entropy captures "unique-looking"; length and a digit/structure bonus lift real ids.
    Deliberately monotone and cheap -- it only has to RANK, not calibrate."""
    n = len(tok)
    if n < 4 or tok in _STOP:
        return 0.0
    counts = Counter(tok)
    entropy = -sum((c / n) * math.log2(c / n) for c in counts.values())  # bits per char
    score = (n - 3) * (0.5 + entropy)
    if any(ch.isdigit() for ch in tok):  # ids / prices / dates discriminate strongly
        score *= 1.6
    if any(ch in "-._:/@%" for ch in tok):  # structured value (guid/date/url/slug)
        score *= 1.3
    return score


class ContentCorrelator:
    """A drop-in refinement of :class:`OrderingCorrelator`: keep its hard temporal gate, then
    NARROW the residual ambiguity by matching response-body values against the node's text.

    For each node the ordering baseline left with several candidates, score each candidate by the
    summed SPECIFICITY of the node-text tokens that appear in THAT candidate's response body -- a
    common word shared by every body cancels out, so dominance emerges only from rare/high-entropy
    values (a GUID, a price, an ISO date, a long unique string) that one body uniquely explains.
    If one candidate clearly dominates (``dominance``x the runner-up and above ``min_score``),
    narrow to it and record a ``confidence``; otherwise the ambiguity is genuine and PRESERVED.
    A node the baseline never precedes is never given a new candidate -- the temporal gate holds."""

    def __init__(
        self, window_s: float = 0.15, *, min_score: float = 6.0, dominance: float = 2.0
    ) -> None:
        self._ordering = OrderingCorrelator(window_s)
        self.min_score = min_score  # a winner must clear this (a lone short-word match ≈ 0)
        self.dominance = dominance  # ...and beat the runner-up by this ratio, else keep ambiguity

    def correlate(
        self, network: "list[NetworkEvent]", dom: "list[DOMUpdateEvent]"
    ) -> Correlation:
        base = self._ordering.correlate(network, dom)  # the temporal gate -- never widened below
        body_tokens: dict[int, set[str]] = {}
        for ev in network:
            if ev.index is None:
                continue
            toks = _body_tokens(ev.body)
            if toks:
                body_tokens[int(ev.index)] = toks
        if not body_tokens:  # no bodies captured -> nothing to refine; the baseline stands
            return base
        node_tokens = self._node_tokens(dom)
        for phase in base.phases:
            if len(phase.candidates) <= 1:
                continue  # already unambiguous (or pre-XHR) -- nothing to narrow
            node_toks = node_tokens.get(phase.node_key)
            if not node_toks:
                continue  # no node text captured -> can't content-match; keep the ambiguity
            self._narrow(phase, node_toks, body_tokens)
        return base

    def _node_tokens(self, dom: "list[DOMUpdateEvent]") -> "dict[str, set[str]]":
        """The union of every text snapshot a node carried across its mutations -> its token set."""
        out: dict[str, set[str]] = {}
        for ev in dom:
            node = ev.node_id or ""
            if not node:
                continue
            text = str((ev.detail or {}).get("text") or "")
            if text:
                out.setdefault(node, set()).update(_text_tokens(text))
        return out

    def _narrow(
        self, phase: DomPhase, node_toks: "set[str]", body_tokens: "dict[int, set[str]]"
    ) -> None:
        scores: list[tuple[float, int]] = []
        for idx in phase.candidates:
            body = body_tokens.get(idx)
            if not body:
                scores.append((0.0, idx))
                continue
            shared = node_toks & body
            scores.append((sum(_specificity(t) for t in shared), idx))
        scores.sort(key=lambda s: (-s[0], s[1]))  # best first, stable by index
        top_score, top_idx = scores[0]
        runner = scores[1][0] if len(scores) > 1 else 0.0
        if top_score >= self.min_score and top_score >= self.dominance * max(runner, 1e-9):
            phase.candidates = [top_idx]  # the value match dominates -> attribute to it
            phase.confidence = min(1.0, top_score / (top_score + runner)) if top_score else None


__all__ = [
    "XhrRequest",
    "DomPhase",
    "Correlation",
    "Correlator",
    "OrderingCorrelator",
    "ContentCorrelator",
]
