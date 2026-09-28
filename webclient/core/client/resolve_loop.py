"""The resolve escalation ladder as a LOOP (roadmap N9): ``browser="auto"`` is
``ResolveLoop`` driving a :class:`ResolveState` with a swappable DRIVER.

Each round the loop OBSERVES the current hop (the request/static flags of the response,
the tiers taken so far, any transport error) and the driver DECIDES the next tier --
``"proxy"`` (a fresh exit via the proxy service), ``"browser"`` (a real render),
``"login"`` (stop: a credential wall no transport fixes), or ``None`` (done: the current
document is the answer). ``apply`` performs the hop. The default driver
(:func:`default_resolve_driver`) is the flag-driven ladder: bot-block at the transport ->
browser; anti-bot challenge -> proxy, then a stealth browser for a named vendor; SPA ->
browser; login wall -> stop. Bounded: static -> (proxy) -> (browser).

Swap the backend per engine with ``wc.driver("resolve", fn)``; a driver may return an
:class:`~webclient.loop.Ask` to hand the decision to a human (the fetch returns the current
hop with ``doc.pending`` set; continue by hand with ``wc.escalate(doc, tier)``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Literal

from pydantic import BaseModel

from ...kernel.errors import make
from ...kernel.loop import Ask, BoundedLoop, LoopVerdict

if TYPE_CHECKING:
    from ..document import Document
    from ..reference import Reference

__all__ = ["ResolveState", "ResolveObservation", "ResolveDriver", "ResolveLoop", "default_resolve_driver", "TIERS"]

Tier = Literal["proxy", "browser", "login"]
TIERS: tuple[str, ...] = ("proxy", "browser")
#: flags whose content only a BROWSER RENDER reveals -- a JS-composed SPA shell, a shadow root, a
#: same-origin iframe. The ONE vocabulary both the resolve ladder and onboarding's write_resolve read
#: (write_resolve bakes a browser for a CONFIRMED source on any of these). They deliberately DIVERGE on
#: what to escalate SPECULATIVELY under ``auto``: AUTO_RENDER_FLAGS is the unambiguous subset (spa /
#: shadow_dom -- content genuinely hidden from static HTML); a bare ``iframe`` is left OUT, since it is
#: too often a benign ad/embed and rendering every page that has an <iframe> would be far too costly.
RENDER_FLAGS: frozenset[str] = frozenset({"spa", "shadow_dom", "iframe"})
AUTO_RENDER_FLAGS: frozenset[str] = frozenset({"spa", "shadow_dom"})
_BOT_BLOCK_HINTS = ("protocol_error", "http2", "connection reset", "reset by peer", "remote protocol")
#: HTTP statuses where a static fetch was BLOCKED but a real browser (a genuine UA + JS + cookies)
#: plausibly gets through -- a browser-only site (403, e.g. Wikipedia / investor.nvidia.com), a
#: throttle or a challenge. Escalated to the browser tier under ``auto``; a 404/410 (genuinely
#: absent) or a 5xx server error is NOT here (a browser would not help).
_BLOCK_STATUSES = frozenset({403, 429, 451, 503})


class ResolveObservation(BaseModel):
    """What the resolve driver sees each round: the URL, the tiers taken, the flags that are
    present (with the anti-bot remedy), and the current hop's error (if any)."""

    url: str = ""
    tiers: list[str] = []
    present: list[str] = []  # the flags present on the current hop
    antibot_remedy: str | None = None
    error: str | None = None  # the current hop's error type, if not ok
    bot_block: bool = False  # the error looks like a protocol-level anti-bot block
    blocked: bool = False  # a blocking HTTP status (403/429/…) -- a browser-only site may still serve it


ResolveDriver = Callable[[ResolveObservation], "Tier | None | Ask"]


@dataclass
class ResolveState:
    """The ladder's mutable state: the reference, the current hop's document/response/flags,
    the tiers taken, and the fetch options that ride along to every hop."""

    ref: Any
    doc: Any
    resp: Any = None
    flags: "dict[str, Any] | None" = None
    tiers: list[str] = field(default_factory=lambda: ["static"])
    headers: dict[str, str] = field(default_factory=dict)
    keep_alive: "bool | float" = False
    wait: Any = None
    tried: set[str] = field(default_factory=set)  # tiers attempted (even if they failed)
    static_doc: Any = None  # the last usable static hop (a fallback when a render fails)


def default_resolve_driver(obs: ResolveObservation) -> "Tier | None | Ask":
    """The built-in flag-driven ladder (see the module docstring)."""
    if (obs.bot_block or obs.blocked) and "browser" not in obs.tiers:
        return "browser"  # a protocol block or a blocking status (403/429/…): a real browser may pass
    if obs.error is not None or (not obs.present and obs.antibot_remedy is None):
        return None  # a failed hop (not a bot block) or a plain page: the current doc is the answer
    if "login_required" in obs.present:
        return "login"
    antibot = "anti_bot_triggered" in obs.present
    if antibot and obs.antibot_remedy in ("proxy", "stealth") and "proxy" not in obs.tiers:
        return "proxy"
    render = any(f in obs.present for f in AUTO_RENDER_FLAGS)  # a JS shell / shadow-root: content is hidden
    if (render or (antibot and obs.antibot_remedy == "stealth")) and "browser" not in obs.tiers:
        return "browser"
    return None


class ResolveLoop:
    """Drive a :class:`ResolveState` through the tiers with ``driver`` (default: the ladder).
    Built on :class:`~webclient.loop.BoundedLoop`, so it publishes ``LoopEvent`` s, can be
    stepped by hand and checkpoints on an :class:`Ask`."""

    name = "resolve"

    def __init__(self, client: Any, *, driver: "ResolveDriver | None" = None, mode: str = "auto") -> None:
        self.client = client
        self.driver = driver or default_resolve_driver
        self.mode = mode
        self.loop: "BoundedLoop[ResolveState, ResolveObservation, Any]" = BoundedLoop(
            observe=self.observe, decide=self.decide, done_result=lambda d: "done" if d is None else None,
            apply=self._apply_sync, max_rounds=len(TIERS) + 2, max_stalls=len(TIERS) + 2,
            name="resolve", bus=getattr(client, "bus", None),
        )
        self._pending_hop: "str | None" = None

    # -- observe / decide ----------------------------------------------------------
    def observe(self, state: ResolveState, round_index: int, error: str) -> ResolveObservation:
        doc = state.doc
        if state.flags is None and doc.error is None and state.resp is not None and self.mode == "auto":
            state.flags = self.client._observe(doc, state.resp)
        present = [n for n, f in (state.flags or {}).items() if f.present]
        antibot = (state.flags or {}).get("anti_bot_triggered")
        err = doc.error
        msg = (getattr(err, "message", "") or "").lower()
        status = getattr(doc, "status_code", None)
        return ResolveObservation(
            url=doc.url, tiers=list(state.tiers), present=present,
            antibot_remedy=antibot.remedy if antibot is not None and antibot.present else None,
            error=err.type if err is not None else None,
            bot_block=err is not None and any(h in msg for h in _BOT_BLOCK_HINTS),
            blocked=err is not None and status in _BLOCK_STATUSES,
        )

    def decide(self, obs: ResolveObservation) -> Any:
        decision = self.driver(obs)
        state = self.loop.state
        if isinstance(decision, str) and state is not None and decision in state.tried:
            return None  # a tier already attempted: the driver would loop -- stop
        return decision

    # -- apply (the hops) ----------------------------------------------------------
    def _apply_sync(self, state: ResolveState, decision: Any) -> None:
        """``apply`` is called from the loop's synchronous step; the hops are async, so the
        loop is driven from within ``run_async`` which awaits the queued hop."""
        self._pending_hop = decision

    async def hop(self, state: ResolveState, tier: str) -> None:
        """Perform one hop: ``proxy`` (re-fetch through a fresh exit), ``browser`` (render; on
        failure fall back to the static hop and put the failure on the ledger), ``login``
        (mark the document with the login-wall error)."""
        from ...policy import ProxyPolicy, Resolve
        from ...policy.headers import policy_headers

        client = self.client
        doc, ref = state.doc, state.ref
        state.tried.add(tier)
        if tier == "login":
            from . import _flag_reason

            doc.error = make(
                "fetch.login_required",
                _flag_reason((state.flags or {}).get("login_required"), "a login wall blocks the content"),
                op="fetch", subject=doc.name,
            )
            return
        if tier == "proxy":
            proxy_headers = {**policy_headers(Resolve(proxy=ProxyPolicy.auto())), **state.headers}
            slot = doc.name
            state.doc, state.resp = await client._afetch_once(ref, proxy_headers)
            client._register(state.doc, ref, reuse=slot)
            state.tiers.append("proxy")
            state.doc._tiers = list(state.tiers)
            state.flags = None  # re-observe the new hop
            return
        if tier == "browser":
            if state.resp is not None and doc.error is None:  # keep the static hop's events
                client._capture(doc, ref, state.resp)
            state.static_doc = doc if doc.error is None else state.static_doc
            try:
                rendered = await client._escalate_to_browser(
                    ref, list(doc._events), doc.content,
                    tiers=[*state.tiers, "browser"], keep_alive=state.keep_alive, wait=state.wait,
                    reuse=doc.name,
                )
            except Exception as exc:  # noqa: BLE001 - a blocked/failed render is not fatal
                doc._note_error(
                    make("fetch.browser_failed", f"{type(exc).__name__}: {exc}",
                         cause=getattr(exc, "error", None)),
                    "fetch",
                )
                state.tiers.append("browser")
                return
            state.tiers.append("browser")
            if rendered is not None and rendered.ok:
                state.doc, state.resp, state.flags = rendered, None, {}
            return
        raise ValueError(f"unknown resolve tier {tier!r}")

    # -- drive ---------------------------------------------------------------------
    async def run_async(self, state: ResolveState) -> "tuple[Any, LoopVerdict]":
        """Drive the ladder to its verdict, returning the resulting document. ``waiting`` (the
        driver asked) returns the current hop with ``doc.pending`` set."""
        self.loop.state = state
        self.loop.round = 0
        verdict = self.loop.step(state)
        while verdict is None:
            hop = self._pending_hop
            self._pending_hop = None
            if hop is not None:
                await self.hop(state, hop)
            verdict = self.loop.step(state)
        if verdict.reason == "waiting":
            state.doc._pending = verdict.ask
            self.client._the_engine().waiting[state.doc.name] = state.doc
        return state.doc, verdict
