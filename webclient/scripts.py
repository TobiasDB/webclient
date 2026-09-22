"""Page scripts: named, registered, togglable JS run at defined points of a page's
lifecycle (roadmap N8).

Every mature stack models script injection as NAMED PHASES (Chrome extensions'
``run_at``, Playwright's ``add_init_script`` vs ``evaluate``, Crawlee's pre/post
navigation hooks), and Crawlee's own history shows why the phase must be explicit. The
phases here, in order of a page's life:

* ``init``   -- installed before every navigation (``add_init_script``): observers, wrappers.
* ``inline`` -- evaluated once after the page settles, BEFORE the HTML snapshot (fold
  shadow DOM / iframes in, stamp interactivity); a returned dict merges into ``dom_stats``.
* ``load``   -- evaluated once after navigation (side effects; result ignored).
* ``drain``  -- evaluated after load AND after every interaction to pull a buffer out of the
  page (the mutation observer, the rrweb recorder); results are keyed by script name on the
  ``PageResult``/drain so a backing reads its own.
* ``unload`` -- evaluated once before the page is closed / released.

A :class:`Script` is a :class:`~webclient.clients.PageScript` with a NAME, an enabled
flag and an ``on`` -- a phase, or a bus TOPIC (``on="action"``): a topic script runs on the
engine loop against the live page of the event's document, never inside the bus handler.
The engine's :class:`ScriptRegistry` gathers them (the backings' declared scripts, the
client's ``inject_script``s, and any registered here), applies the :class:`ScriptPolicy`
(allow / deny / default), and every run publishes a :class:`~webclient.models.ScriptEvent`.

    wc.scripts.register(Script("my-probe", "() => document.title", on="load"))
    wc.scripts.disable("wc.rrweb")            # toggle by name
    wc.scripts.policy = ScriptPolicy(deny=["wc.interactivity"])
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterable, List, Literal

from .clients.browser import PageScript, Phase

if TYPE_CHECKING:
    from .events import EventBus

log = logging.getLogger(__name__)

__all__ = ["Script", "ScriptPolicy", "ScriptRegistry", "PHASES", "Phase"]

PHASES: tuple[str, ...] = ("init", "inline", "load", "drain", "unload")


@dataclass
class Script:
    """A named page script. ``on`` is a lifecycle phase (see :data:`PHASES`) or a bus topic
    prefix (``"action"`` / ``"network.navigation"`` / ...): a topic script is evaluated on
    the live page of the event's document each time such an event is published."""

    name: str
    source: str
    on: str = "init"
    enabled: bool = True
    owner: str = ""  # who declared it (a backing's class name / "client" / "user")
    description: str = ""

    @property
    def phase(self) -> "Phase | None":
        """The lifecycle phase, or ``None`` for a topic-triggered script."""
        return self.on if self.on in PHASES else None  # type: ignore[return-value]

    @property
    def topic(self) -> "str | None":
        return None if self.on in PHASES else self.on

    def page_script(self) -> PageScript:
        """The low-level unit the browser client installs (named, so runs can be reported)."""
        return PageScript(self.source, self.phase or "load", name=self.name)


@dataclass
class ScriptPolicy:
    """Engine-level governance: ``default`` runs every enabled script unless denied
    (``"on"``) or NO script unless allowed (``"off"``); ``allow`` / ``deny`` are name
    prefixes (``"wc."`` covers every built-in)."""

    default: Literal["on", "off"] = "on"
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()

    def permits(self, name: str) -> bool:
        if any(name.startswith(d) for d in self.deny):
            return False
        if any(name.startswith(a) for a in self.allow):
            return True
        return self.default == "on"


class ScriptRegistry:
    """The engine's script table: register / enable / disable / list, the policy, and the
    gather step the browser path calls to get the scripts to install for a page."""

    def __init__(self, *, bus: "EventBus | None" = None) -> None:
        self._scripts: dict[str, Script] = {}
        self.policy = ScriptPolicy()
        self._bus = bus
        self._trigger: "Callable[[Any], None] | None" = None  # the client's topic-script runner
        self._subscribed = False
        self._user_n = 0

    # -- registration ----------------------------------------------------------
    def register(self, script: Script) -> Script:
        """Register (or replace, by name) a script. A topic script arms the bus trigger."""
        self._scripts[script.name] = script
        if script.topic is not None:
            self._arm()
        return script

    def inject(self, source: str, phase: str = "init", *, name: str = "") -> Script:
        """Register a user script (the ``inject_script`` back door) under an auto name."""
        self._user_n += 1
        return self.register(Script(name or f"user.{self._user_n}", source, on=phase, owner="user"))

    def declare(self, owner: str, scripts: "Iterable[Any]") -> None:
        """Register a backing's declared ``page_scripts`` (``Script``s, or bare ``PageScript``s
        auto-named ``<owner>.<phase>[.<i>]``) -- idempotent per name."""
        counts: dict[str, int] = {}
        for s in scripts:
            if isinstance(s, Script):
                if s.name not in self._scripts:
                    self.register(Script(**{**s.__dict__, "owner": s.owner or owner}))
                continue
            phase = getattr(s, "phase", "init")
            counts[phase] = counts.get(phase, 0) + 1
            name = getattr(s, "name", "") or f"{owner}.{phase}" + (f".{counts[phase]}" if counts[phase] > 1 else "")
            if name not in self._scripts:
                self.register(Script(name, s.source, on=phase, owner=owner))

    def get(self, name: str) -> "Script | None":
        return self._scripts.get(name)

    def list(self) -> "List[Script]":
        return [*self._scripts.values()]

    def enable(self, name: str) -> None:
        self._scripts[name].enabled = True

    def disable(self, name: str) -> None:
        self._scripts[name].enabled = False

    def active(self, script: Script) -> bool:
        """Whether a script runs now: enabled AND permitted by the policy."""
        return script.enabled and self.policy.permits(script.name)

    # -- the gather step ---------------------------------------------------------
    def gather(self, *, extra: "Iterable[Any]" = ()) -> "List[PageScript]":
        """The page scripts to install on a live page: every active phase script (plus
        ``extra`` one-offs), as :class:`PageScript`s in registration order."""
        out = [s.page_script() for s in self._scripts.values() if s.phase is not None and self.active(s)]
        out.extend(extra)
        return out

    def for_topic(self, topic: str) -> "List[Script]":
        """The active topic scripts whose ``on`` prefix-matches ``topic``."""
        from .models import topic_matches

        return [s for s in self._scripts.values()
                if s.topic is not None and self.active(s) and topic_matches(s.topic, topic)]

    # -- events ------------------------------------------------------------------
    def bind(self, bus: "EventBus", trigger: "Callable[[Any], None]") -> None:
        """Attach the bus (for ScriptEvents) and the client's topic-script runner."""
        self._bus = bus
        self._trigger = trigger
        if any(s.topic is not None for s in self._scripts.values()):
            self._arm()

    def _arm(self) -> None:
        """Subscribe the trigger to the bus once, only when a topic script exists."""
        if self._subscribed or self._bus is None or self._trigger is None:
            return
        self._bus.subscribe("", self._on_event)
        self._subscribed = True

    def _on_event(self, event: Any) -> None:
        if event.topic == "script" or self._trigger is None:  # never trigger on our own events
            return
        if self.for_topic(event.topic):
            self._trigger(event)

    def report(self, name: str, phase: str, *, document_id: "str | None" = None, result: Any = None,
               error: "str | None" = None) -> None:
        """Publish a :class:`~webclient.models.ScriptEvent` for one run (a no-op without a bus)."""
        if self._bus is None:
            return
        from .models import ScriptEvent

        detail: dict[str, Any] = {}
        if error:
            detail["error"] = error
        elif result is not None:
            detail["result"] = _summary(result)
        self._bus.publish(ScriptEvent(script=name, phase=phase, detail=detail, document_id=document_id))


def _summary(value: Any) -> Any:
    """A small JSON-friendly summary of a script's result (counts for containers)."""
    if isinstance(value, dict):
        return {k: (len(v) if isinstance(v, (list, dict, str)) else v) for k, v in list(value.items())[:12]}
    if isinstance(value, list):
        return {"items": len(value)}
    if isinstance(value, str):
        return value[:120]
    return value
