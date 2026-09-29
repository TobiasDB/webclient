"""Policies -- declarative, serialisable config objects that BUILD their middleware.

Each resolve slot is a Policy: a small pydantic model that says WHAT to do (``RetryPolicy(max_
attempts=5)``), and ``.build(pool)`` turns it into the configured ``web.fetch.Middleware``. Because
policies are plain models, a :class:`~web.resolve.Profile` (a bundle of them) stays fully
serialisable and self-contained -- no ready-made middleware closures baked in. The transport
ladder is itself a policy: :class:`EscalationPolicy` holds the tiers (fetch identities to climb)
and ``on`` (which failures trigger a climb).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from web.fetch import ClientPool, Fetcher, Fingerprint, Middleware, Snapshot
from web.fetch import Profile as FetchProfile


@runtime_checkable
class Policy(Protocol):
    """A slot policy: ``build(pool)`` turns this declarative config into a configured middleware.
    (``EscalationPolicy`` is special -- it also supplies the base tier -- so it is not a ``Policy``.)"""

    def build(self, pool: ClientPool) -> Middleware: ...

from .middleware import escalate as _escalate
from .middleware import rate_limit as _rate_limit
from .middleware import retry as _retry
from .middleware import rotate as _rotate
from .paginate import paginate_param


class RetryPolicy(BaseModel):
    """Retry the same request while its Snapshot is retriable (transport error / 429 / 5xx)."""

    max_attempts: int = 3
    backoff: float = 0.2

    def build(self, pool: ClientPool) -> Middleware:
        return _retry(self.max_attempts, self.backoff)


class RatePolicy(BaseModel):
    """Keep at least ``per_host`` seconds between requests to the same host (politeness)."""

    per_host: float = 0.5

    def build(self, pool: ClientPool) -> Middleware:
        return _rate_limit(self.per_host)


class RotationPolicy(BaseModel):
    """Present a fresh identity per request -- lease a differently-fingerprinted backend from the
    pool (a browserforge ``fleet``; empty = the default fleet). Only sane WITH IP rotation."""

    fleet: "tuple[Fingerprint, ...]" = ()

    def build(self, pool: ClientPool) -> Middleware:
        return _rotate(pool, self.fleet or None)


class PaginatePolicy(BaseModel):
    """Walk a ``?param=N`` paginated dataset, merging up to ``max_pages`` pages into one document."""

    param: str
    max_pages: int = 5

    def build(self, pool: ClientPool) -> Middleware:
        return paginate_param(self.param, max_pages=self.max_pages)


class EscalationPolicy(BaseModel):
    """The transport LADDER as a policy: ``tiers`` are the fetch identities to climb (base first,
    then the tiers to escalate to on a block), and ``on`` names the triggers -- status codes
    (``"403"``), error codes (``"fetch.timeout"``), or ``"blocked"`` for the default heuristic
    (bad status / anti-bot / JS-gated). Empty ``on`` uses that heuristic."""

    tiers: "tuple[FetchProfile, ...]" = ()
    on: "tuple[str, ...]" = ()

    def base(self, pool: ClientPool) -> Fetcher:
        """The base tier (``tiers[0]``, leased), or the minimal default identity when unset."""
        return pool.lease(self.tiers[0]) if self.tiers else pool.lease(FetchProfile())

    def build(self, pool: ClientPool) -> "Middleware | None":
        """The escalate middleware over the tiers ABOVE the base, or ``None`` when there are none."""
        climb = [pool.lease(t) for t in self.tiers[1:]]
        return _escalate(climb, blocked=_triggers(self.on)) if climb else None


def _triggers(on: "tuple[str, ...]") -> "Callable[[Snapshot], bool] | None":
    """A blocked-predicate from ``on`` tokens (status / error codes / ``"blocked"``); ``None`` (the
    default heuristic) when ``on`` is empty."""
    if not on:
        return None
    tokens = frozenset(on)

    def blocked(snap: Snapshot) -> bool:
        if str(snap.status) in tokens:
            return True
        if snap.error is not None and snap.error.code in tokens:
            return True
        return "blocked" in tokens and (not snap.ok)

    return blocked


__all__ = ["Policy", "RetryPolicy", "RatePolicy", "RotationPolicy", "PaginatePolicy", "EscalationPolicy"]
