"""Errors: the ``WebError`` value (problem-details shaped), the catalogue of error codes,
and the RAISE/RETURN policy markers.

A failed fetch/resolve is loud by default (RAISE); ``error=RETURN`` (or ``optional=True``)
instead yields a not-ok ``Document`` carrying a serialisable ``WebError``. Value ops
(``is_ok`` / ``is_empty`` / ``error`` / ``message``) work on a not-ok document too.

**Shape** (RFC 9457 *Problem Details*, extended for agents): every ``WebError`` carries a
catalogued ``code`` (``fetch.http_status``, ``select.no_match``, …), a human ``title``, the
``message`` (RFC ``detail``), the HTTP-ish ``status_code``, whether it is ``retriable``, a
machine-readable ``remedy`` from a CLOSED vocabulary (:data:`REMEDIES`) and a one-sentence
``hint`` -- so an autonomous caller branches on ``code`` / ``retriable`` / ``remedy`` and never
parses prose. ``op`` and ``subject`` bind the error to the operation and the object (a
document / reference / crawl name) it occurred on; ``cause`` nests an underlying error.
:meth:`WebError.problem` renders the RFC 9457 dict (``application/problem+json``).

**Catalogue**: :data:`CATALOG` is the single list of codes with their defaults; build an
error with :func:`make` (``make("fetch.login_required", message=...)``), and
``scripts/gen_docs.py errors`` renders ``docs/errors.md`` from it. ``type`` is the legacy
short kind (``HTTPStatus`` / ``TransportError`` / ``LookupError`` / …) callers and tests
already branch on; it is derived from the catalogue entry and kept stable.
"""

from __future__ import annotations

import contextlib as _contextlib
import contextvars as _contextvars
from collections.abc import Iterator as _Iterator
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, PrivateAttr


class _Policy:
    """A named error-policy marker (identity-compared)."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        """The policy's name (``RAISE`` / ``RETURN``)."""
        return self.name


RAISE: _Policy = _Policy("RAISE")  # default: a failed fetch/resolve raises
RETURN: _Policy = _Policy("RETURN")  # lenient: return a not-ok document

#: the ambient error policy: RAISE at the top level; extract/filter run their
#: sub-expressions under RETURN so one bad field never aborts a whole plan.
_CURRENT: "_contextvars.ContextVar[_Policy]" = _contextvars.ContextVar(
    "webclient_policy", default=RAISE
)


def current_policy() -> "_Policy":
    """The ambient error policy in effect for the current context (RAISE at top level)."""
    return _CURRENT.get()


def lenient(optional: bool, error: object) -> bool:
    """Whether a failed fetch/resolve should return a not-ok document rather than
    raise -- ``optional=True`` or ``error=RETURN``. The one place the two spellings
    of leniency are reconciled (used by ``fetch`` and ``resolve``)."""
    return optional or error is RETURN


@_contextlib.contextmanager
def default_policy(policy: "_Policy") -> "_Iterator[None]":
    """Set the ambient error policy for the duration of the ``with`` block (restored on exit) --
    how ``extract``/``filter`` run their sub-expressions under RETURN."""
    token = _CURRENT.set(policy)
    try:
        yield
    finally:
        _CURRENT.reset(token)


# --------------------------------------------------------------------------- #
# the remedy vocabulary + the catalogue
# --------------------------------------------------------------------------- #

Remedy = Literal[
    "retry",  # the same request may succeed later (transport blip / 429 / 5xx)
    "browser",  # re-resolve with browser="always" (JS-gated content)
    "proxy",  # re-resolve through a fresh proxy exit (a bare block)
    "stealth",  # re-resolve in a stealth/fingerprinted browser (a named anti-bot vendor)
    "credentials",  # a login wall: supply a session with credentials
    "fix_selector",  # the selector/attribute/path does not match -- re-read the skeleton
    "fix_plan",  # the plan/request is malformed -- correct it before re-running
    "reproduce",  # a server-side handle is gone -- re-run the plan that produced it
    "reduce_load",  # a resource cap is hit -- close/free something or wait
    "unsupported",  # the op is not available on this object -- pick another op
    "none",  # nothing to do: the target is unavailable or refused by policy
]
#: the closed remedy vocabulary (the values of :data:`Remedy`), for docs and validation.
REMEDIES: tuple[str, ...] = (
    "retry", "browser", "proxy", "stealth", "credentials", "fix_selector", "fix_plan",
    "reproduce", "reduce_load", "unsupported", "none",
)


@dataclass(frozen=True)
class ErrorSpec:
    """A catalogue entry: the defaults an error code carries."""

    code: str
    type: str  # the legacy short kind callers branch on (kept stable)
    title: str
    remedy: str
    hint: str
    retriable: bool = False
    status_code: int = 0  # the default HTTP-ish status (0 = none / transport)
    doc: str = ""  # a longer explanation for the generated docs


def _spec(*a: Any, **k: Any) -> ErrorSpec:
    return ErrorSpec(*a, **k)


#: every error code the package can produce, with its defaults. ADD HERE when a new
#: failure mode appears; ``tests/test_errors.py`` checks the codes used in the package
#: are all catalogued and ``scripts/gen_docs.py errors`` renders ``docs/errors.md``.
CATALOG: dict[str, ErrorSpec] = {s.code: s for s in (
    # -- fetch / resolve ---------------------------------------------------
    _spec("fetch.transport", "TransportError", "Transport failure", "retry",
          "The request never got a response (DNS, TLS, reset, timeout); retry, then consider a browser.",
          retriable=True, doc="Raised for connection-level failures. Under ``browser=\"auto\"`` a protocol-level reset is treated as an anti-bot tell and escalated to a browser."),
    _spec("fetch.http_status", "HTTPStatus", "Non-2xx response", "none",
          "The server answered with an error status; 429/5xx are retriable, a 4xx is not.",
          doc="``retriable`` mirrors ``RetryPolicy.on_statuses`` (429, 500, 502, 503, 504)."),
    _spec("fetch.blocked_host", "BlockedHost", "Host refused by the SSRF guard", "none",
          "The host resolves to a loopback/private/link-local address and block_private_hosts is on.",
          doc="Only when ``WebClient(block_private_hosts=True)``; the request is refused before any transport."),
    _spec("fetch.login_required", "LoginRequired", "Login wall", "credentials",
          "A credential wall blocks the content; no transport tier fixes this -- fetch through a session that is logged in.",
          doc="Detected by the ``login_required`` flag under ``browser=\"auto\"``."),
    _spec("fetch.browser_failed", "BrowserError", "Browser render failed", "retry",
          "The browser could not launch or render the page; retry, or fall back to the static tier.",
          retriable=True, doc="Under ``browser=\"auto\"`` the client falls back to the ok static hop and records this on the error ledger; under ``browser=\"always\"`` it surfaces."),
    _spec("fetch.anti_bot", "AntiBot", "Anti-bot challenge", "stealth",
          "The site is challenging or blocking this client; re-resolve via a proxy exit or a stealth browser.",
          retriable=True),
    # -- selection / rendering / dispatch ----------------------------------
    _spec("select.no_match", "LookupError", "Selector matched nothing", "fix_selector",
          "No element matches the selector; re-read the skeleton and pick a selector it shows.",
          doc="A ``SelectError`` under RAISE, else a not-ok element document. Not retriable: the selector will not start matching."),
    _spec("select.no_attribute", "LookupError", "Attribute absent", "fix_selector",
          "The element has no such attribute; check the skeleton's attribute list for the node."),
    _spec("select.no_path", "LookupError", "JSON path missing", "fix_selector",
          "The dotted path does not exist in the JSON value; read the JSON skeleton for the real keys."),
    _spec("select.not_json", "LookupError", "Not JSON", "fix_selector",
          "The element's text is not valid JSON; as_json() needs a JSON island (a script[type=application/json] blob)."),
    _spec("render.unknown_format", "RenderError", "Unknown render format", "fix_plan",
          "No such render format for this document kind; use markdown / text / html / links / elements / skeleton."),
    _spec("paginate.not_live", "NotLive", "Interacted pager needs a live page", "browser",
          "paginate(click=...) / paginate(scroll=True) load more on a HELD browser page; re-resolve with browser=True.",
          doc="Raised by a click / scroll pager on a static document (no browser page is held)."),
    _spec("paginate.invalid", "InvalidPager", "Pager malformed", "fix_plan",
          "A pager is exactly ONE iterator -- next= / pages= / cursor= (+ param=) / click= / scroll=True -- plus optional until= / filter= / max_pages= / records=.",
          status_code=422,
          doc="Raised by ``paginate(...)`` with no iterator, two iterators, a cursor without ``param=``, or a removed ``by=``-API kwarg (the message says what to write instead)."),
    _spec("op.unsupported", "UnsupportedOp", "Op not available here", "unsupported",
          "This object's current state has no such op; check its capabilities and pick another op.",
          doc="Raised by dispatch when no chosen backing provides the op (e.g. ``markdown()`` on a JSON document)."),
    # -- plans / service -----------------------------------------------------
    _spec("plan.invalid", "InvalidPlan", "Plan rejected", "fix_plan",
          "The plan's root, operator or step names are not valid; names starting with '_' and unknown roots/operators are refused before dispatch.",
          status_code=422),
    _spec("request.invalid", "InvalidRequest", "Request malformed", "fix_plan",
          "The request body is missing a required field; see the hint for the expected shape.",
          status_code=422),
    _spec("service.no_document", "NoSuchDocument", "Document handle gone", "reproduce",
          "The server-side handle expired or its session closed; re-run the plan that produced it to get a fresh handle.",
          retriable=True, status_code=404),
    _spec("service.no_session", "NoSuchSession", "Session gone", "reproduce",
          "The session expired or was closed; create a new one (POST /sessions).",
          status_code=404),
    _spec("service.too_many_sessions", "TooManySessions", "Session store full", "reduce_load",
          "The session store is at capacity; close an existing session or retry after in-flight sessions expire.",
          retriable=True, status_code=429),
    _spec("remote.failed", "RemoteError", "Remote call failed", "retry",
          "The service returned a non-2xx response; the nested cause carries its structured error when it sent one.",
          retriable=True, status_code=502),
    # -- resources -----------------------------------------------------------
    _spec("pool.exhausted", "PoolExhausted", "No transport lease", "reduce_load",
          "No http/page client became free within the acquire timeout; release held pages or raise the pool limit.",
          retriable=True),
    _spec("loop.failed", "LoopError", "Loop step failed", "none",
          "A loop's apply step raised; the verdict carries the message and the loop stopped."),
    # -- replay ----------------------------------------------------------------
    _spec("replay.offline", "Offline", "No network in static replay", "none",
          "This document was rebuilt from a trace; static replay never fetches -- use replay=\"har\" (WebClient(har=...)) or a live client to re-resolve."),
    _spec("replay.har_miss", "HarMiss", "Request not in the HAR", "fix_plan",
          "The replay's HAR has no entry for this request; the plan drifted from its recording -- re-record, or run it live.",
          status_code=599),
    # -- crawl -----------------------------------------------------------------
    _spec("crawl.robots_disallowed", "RobotsDisallowed", "Disallowed by robots.txt", "none",
          "The site's robots.txt disallows this URL for us; skip it (or crawl with obey_robots=False if you are entitled to)."),
    _spec("crawl.edge_failed", "CrawlEdgeFailed", "Crawl edge failed", "retry",
          "One frontier edge could not be fetched or expanded; the crawl continued -- see crawl.failures and the cause.",
          retriable=True),
)}


def spec(code: str) -> ErrorSpec:
    """The catalogue entry for ``code`` (``KeyError`` for an unknown code -- add it to
    :data:`CATALOG`)."""
    return CATALOG[code]


class WebError(BaseModel):
    """A serialisable failure (problem-details shaped) attached to a not-ok document or
    carried by a :class:`WebException`. ``retriable`` is an actionable hint for clients/
    agents: transport failures, rate limits (429) and server errors (5xx) may succeed on
    retry; a 4xx or a bad selector will not. ``remedy`` names the next action from the
    closed :data:`REMEDIES` vocabulary."""

    type: str = "http_error"  # the legacy short kind (HTTPStatus / TransportError / …)
    message: str = ""  # RFC 9457 ``detail``
    status_code: int = 0
    retriable: bool = False
    # -- problem-details extensions (agent-actionable) ----------------------
    code: str = ""  # the catalogued code (``fetch.http_status``); "" for an ad-hoc error
    title: str = ""  # RFC 9457 ``title``
    remedy: str | None = None  # one of REMEDIES, or None when nothing is prescribed
    hint: str = ""  # one sentence of guidance
    op: str = ""  # the op that produced it (fetch / select / attr / render / execute …)
    subject: str = ""  # the document / reference / crawl name it is bound to
    cause: "WebError | None" = None  # a nested underlying error
    #: set once the error has been published on an engine's bus (the ledger), so a raise
    #: that passes through several dispatch frames is recorded exactly once.
    _noted: bool = PrivateAttr(default=False)

    def problem(self, *, instance: str | None = None) -> dict[str, Any]:
        """The RFC 9457 *Problem Details* dict: ``type`` (a ``urn:webclient:error:<code>``
        URI), ``title``, ``status``, ``detail``, optional ``instance``, plus the extension
        members (``code`` / ``retriable`` / ``remedy`` / ``hint`` / ``op`` / ``subject`` /
        ``cause``). Serve it as ``application/problem+json``."""
        body: dict[str, Any] = {
            "type": f"urn:webclient:error:{self.code or self.type}",
            "title": self.title or self.type,
            "status": self.status_code or None,
            "detail": self.message,
            "code": self.code or self.type,
            "kind": self.type,
            "retriable": self.retriable,
        }
        if instance is not None:
            body["instance"] = instance
        for key in ("remedy", "hint", "op", "subject"):
            value = getattr(self, key)
            if value:
                body[key] = value
        if self.cause is not None:
            body["cause"] = self.cause.problem()
        return {k: v for k, v in body.items() if v is not None}

    def bound(self, *, op: str = "", subject: str = "") -> "WebError":
        """A copy bound to the ``op`` / ``subject`` it occurred on (only fills empty fields)."""
        return self.model_copy(update={
            "op": self.op or op, "subject": self.subject or subject,
        })


def make(code: str, message: str = "", **overrides: Any) -> WebError:
    """Build a catalogued :class:`WebError`: the entry's ``type`` / ``title`` / ``remedy`` /
    ``hint`` / ``retriable`` / ``status_code`` defaults, ``message`` (falling back to the
    title), and any explicit ``overrides`` (e.g. ``status_code=``, ``op=``, ``cause=``)."""
    s = spec(code)
    fields: dict[str, Any] = {
        "type": s.type, "code": s.code, "title": s.title, "remedy": s.remedy,
        "hint": s.hint, "retriable": s.retriable, "status_code": s.status_code,
        "message": message or s.title,
    }
    fields.update(overrides)
    return WebError(**fields)


class WebException(Exception):
    """Raised by a loud (RAISE) fetch/resolve; carries the ``WebError`` and the
    not-ok ``document`` (when one was built)."""

    def __init__(self, error: WebError, document: object | None = None) -> None:
        super().__init__(error.message or f"HTTP {error.status_code}")
        self.error = error
        self.document = document


#: a fetch/resolve failure -- the name used at the call sites/tests.
FetchError = WebException


class SelectError(WebException, LookupError):
    """A miss on a data-navigation op (``select`` / ``attr`` / a live ``select``).
    A :class:`WebException` -- so ONE ``except WebException`` catches fetch failures
    *and* selection misses, and ``.error`` is always a structured ``WebError`` with
    ``.type``/``.retriable`` -- while remaining a ``LookupError`` for back-compat
    with ``except LookupError``. Not retriable (the selector won't start matching)."""


def select_error(
    message: str, *, selector: str | None = None, code: str = "select.no_match"
) -> SelectError:
    """Build a structured :class:`SelectError` for a missing selection/attribute/path."""
    return SelectError(make(code, message, op="select"))


class RenderError(WebException, LookupError):
    """An unknown / unavailable render format (e.g. ``render("pdf")``). A
    ``WebException`` (so ``except WebException`` catches it) and a ``LookupError``
    (back-compat)."""


def render_error(message: str) -> RenderError:
    """Build a structured :class:`RenderError` for an unknown/unavailable render format."""
    return RenderError(make("render.unknown_format", message, op="render"))


class RemoteError(Exception):
    """A remote ``/execute`` call returned a non-2xx response. ``error`` carries
    the server's structured ``WebError`` when it sent one (so ``.error.retriable``
    works the same as on a local failure)."""

    def __init__(
        self, status_code: int, detail: str = "", error: "WebError | None" = None
    ) -> None:
        super().__init__(f"remote execute failed: {status_code} {detail}".strip())
        self.status_code = status_code
        self.error = error


#: statuses worth a retry -- MIRRORS ``RetryPolicy.on_statuses`` (the declared
#: policy), so the local retry loop and the policy don't disagree (501/505/507 are
#: not retriable). Transport failures (status 0) are always retriable.
_RETRIABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


def error_for(status_code: int, message: str = "") -> WebError:
    """Classify an HTTP status into a ``WebError`` (incl. whether it is worth a
    retry: transport failures + the ``_RETRIABLE_STATUSES`` set)."""
    if status_code == 0:
        return make("fetch.transport", message or "the request got no response", op="fetch")
    retriable = status_code in _RETRIABLE_STATUSES
    return make(
        "fetch.http_status",
        message or f"HTTP {status_code} for the request",
        status_code=status_code,
        retriable=retriable,
        remedy="retry" if retriable else "none",
        op="fetch",
    )


__all__ = [
    "RAISE",
    "RETURN",
    "WebError",
    "WebException",
    "SelectError",
    "select_error",
    "RenderError",
    "render_error",
    "FetchError",
    "RemoteError",
    "error_for",
    "current_policy",
    "default_policy",
    "CATALOG",
    "ErrorSpec",
    "REMEDIES",
    "Remedy",
    "make",
    "spec",
]
