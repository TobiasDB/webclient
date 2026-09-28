"""Logging for the package -- standard :mod:`logging`, zero cost when off.

Every module logs through ``logging.getLogger(__name__)`` (so ``webclient.core.client``,
``webclient.clients.pool``, … are the switchboard), uses ``%``-style lazy arguments (never
an f-string), and guards anything expensive to render with ``log.isEnabledFor(DEBUG)``.
The package root ``webclient`` carries a :class:`logging.NullHandler`, so a library user
who never configures logging sees nothing and pays nothing.

Levels, by convention:

* ``DEBUG``  -- the hot paths, narrated: each fetch / lease / round / plan step.
* ``INFO``   -- decisions: a tier escalation, a crawl round, a loop verdict, a service start.
* ``WARNING``-- something degraded but continued (a swallowed hop, a retry, a nag).
* ``ERROR``  -- a failure the caller will see (raised / returned as a not-ok document).

Logs are for humans; the event bus is for machines (roadmap D7). :func:`log_events`
bridges the two in ONE direction -- it subscribes to a bus and narrates each event at
``DEBUG`` -- so ``WEBCLIENT_LOG_LEVEL=DEBUG`` shows the stream without a second
instrumentation path. Nothing is logged that is *only* an event, and nothing is turned
into an event because it was logged.

    from webclient.log import configure_logging
    configure_logging("DEBUG")            # or Settings(log_level="DEBUG").configure_logging()
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .events import EventBus, Subscription

ROOT = "webclient"
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

logging.getLogger(ROOT).addHandler(logging.NullHandler())


def configure_logging(
    level: "int | str | None" = "INFO", *, stream: Any = None, fmt: str = _FORMAT
) -> logging.Logger:
    """Attach ONE stream handler to the ``webclient`` logger at ``level`` (idempotent -- a
    second call re-levels the existing handler rather than stacking another). ``level=None``
    leaves the handlers alone and only returns the logger. Meant for scripts / CLIs; an
    application that has its own logging config should not call this."""
    logger = logging.getLogger(ROOT)
    if level is None:
        return logger
    lvl = logging.getLevelName(level.upper()) if isinstance(level, str) else level
    if not isinstance(lvl, int):
        raise ValueError(f"unknown log level {level!r}")
    handler = next(
        (h for h in logger.handlers if getattr(h, "_webclient_configured", False)), None
    )
    if handler is None:
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter(fmt))
        handler._webclient_configured = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    handler.setLevel(lvl)
    logger.setLevel(lvl)
    return logger


def log_events(bus: "EventBus", *, level: int = logging.DEBUG, topic: str = "") -> "Subscription":
    """Narrate a bus's events to the ``webclient.events`` logger, one line each at ``level``
    (``topic`` filters by dotted prefix). Returns the subscription -- ``cancel()`` it to stop.
    The handler is a no-op when the level is off, so leaving it subscribed costs one
    ``isEnabledFor`` check per event."""
    logger = logging.getLogger(f"{ROOT}.events")

    def handler(event: Any) -> None:
        if not logger.isEnabledFor(level):
            return
        logger.log(level, "%s #%s doc=%s %s", event.topic, event.seq, event.document_id, _brief(event))

    return bus.subscribe(topic, handler)


def _brief(event: Any) -> str:
    """A one-line, lazily-built summary of an event's payload (only called when enabled)."""
    data = event.model_dump(exclude={"topic", "seq", "ts", "document_id", "session_id",
                                     "plan_id", "node_id", "source", "body", "request"},
                            exclude_none=True, exclude_defaults=True)
    req = getattr(event, "request", None)
    if req is not None:
        try:
            data["url"] = req.dispatch("url")
        except Exception:  # noqa: BLE001 - a bare reference without a client
            pass
    return " ".join(f"{k}={v!r}"[:120] for k, v in data.items())


__all__ = ["configure_logging", "log_events", "ROOT"]
