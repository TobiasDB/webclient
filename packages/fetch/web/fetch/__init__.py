"""web.fetch -- the transport layer: ``Request -> Snapshot``.

Perform a request and report the raw response as a :class:`Snapshot` (request + bytes +
transport metadata + any captured events). It does transport ONLY -- it never sniffs the
content kind or decodes a charset (that is web.parse), and it never raises for a transport
failure (that becomes ``snapshot.error``). Usable in isolation, depending only on httpx:

    from web.fetch import Request, HttpFetcher
    snap = await HttpFetcher().fetch(Request(url="https://example.com"))

The :class:`Fetcher` interface is the boundary every transport implements (static HTTP here;
a browser / HAR replay are further fetchers). A sync / lazy / remote face is the DSL's job,
not this layer's.
"""

from __future__ import annotations

from . import (  # named default transport identities (web.fetch.profiles.BASIC / BROWSER / ...)
    profiles,
)
from .browser import (
    DEEP_DOM,
    DOM_RECORDER,
    OPEN_SHADOW,
    BrowserFetcher,
    BrowserManager,
    BrowserSession,
    BrowserSupply,
    CdpSupply,
    LaunchSupply,
    ScriptRegistry,
    ensure_chromium,
    real_chrome_path,
    supply_for,
)
from .bus import Event, EventBus, Subscription, Trace, emit, topic_matches, using
from .entry import Profile  # functional entry + pool
from .entry import (
    ClientPool,
    Entry,
    aclose_default_pool,
    default_pool,
    fetch,
)
from .errors import WebError, WebException, err
from .fingerprint import CHROME, Fingerprint, fleet, generate
from .http import HttpFetcher, HttpSession
from .impersonate import ImpersonateFetcher, ImpersonateSession
from .middleware import Handler, Middleware, stack
from .models import ConsoleEvent  # the plain data models + interfaces
from .models import (
    DOMEvent,
    Fetcher,
    FetchEvent,
    NetworkEvent,
    Request,
    Script,
    Session,
    Snapshot,
    Wait,
)
from .proxy import Proxy
from .replay import Recorder, ReplayBackend

__all__ = [
    "Request",
    "Snapshot",
    "Fetcher",
    "HttpFetcher",
    "BrowserFetcher",
    "BrowserSession",
    "BrowserManager",
    "ImpersonateFetcher",
    "ImpersonateSession",
    "BrowserSupply",
    "LaunchSupply",
    "CdpSupply",
    "supply_for",
    "real_chrome_path",
    "ensure_chromium",
    "HttpSession",
    "Session",
    "fetch",
    "Entry",
    "Profile",
    "profiles",
    "ClientPool",
    "default_pool",
    "aclose_default_pool",
    "Script",
    "ScriptRegistry",
    "Proxy",
    "Fingerprint",
    "CHROME",
    "fleet",
    "generate",
    "Wait",
    "DOM_RECORDER",
    "OPEN_SHADOW",
    "DEEP_DOM",
    "NetworkEvent",
    "DOMEvent",
    "FetchEvent",
    "ConsoleEvent",
    "Middleware",
    "Handler",
    "stack",
    "Recorder",
    "ReplayBackend",
    # the shared substrate (relocated from the removed web.kernel bottom layer)
    "WebError",
    "WebException",
    "err",
    "Event",
    "EventBus",
    "Subscription",
    "topic_matches",
    "emit",
    "using",
    "Trace",
]
