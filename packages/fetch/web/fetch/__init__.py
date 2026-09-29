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

from .models import (ConsoleEvent, DOMEvent, Fetcher, FetchEvent, NetworkEvent, Request, Script,
                     Session, Snapshot, Wait)  # the plain data models + interfaces
from .browser import BrowserFetcher, BrowserSession
from .bus import Event, EventBus, Subscription, Trace, emit, topic_matches, using
from .errors import WebError, WebException, err
from .fingerprint import CHROME, Fingerprint
from .script import DOM_RECORDER, ScriptRegistry
from .http import HttpFetcher, HttpSession
from .middleware import Handler, Middleware, stack
from .replay import Recorder, ReplayBackend
from .pool import Pool
from .proxy import Proxy
from .entry import ClientPool, Entry, Profile, aclose_default_pool, default_pool, fetch  # functional entry + pool

__all__ = ["Request", "Snapshot", "Fetcher", "HttpFetcher", "BrowserFetcher", "BrowserSession", "HttpSession", "Session",
           "fetch", "Entry", "Profile", "ClientPool", "default_pool", "aclose_default_pool",
           "Script", "ScriptRegistry", "Proxy", "Pool", "Fingerprint", "CHROME", "Wait",
           "DOM_RECORDER", "NetworkEvent", "DOMEvent", "FetchEvent", "ConsoleEvent", "Middleware", "Handler", "stack",
           "Recorder", "ReplayBackend",
           # the shared substrate (relocated from the removed web.kernel bottom layer)
           "WebError", "WebException", "err", "Event", "EventBus", "Subscription", "topic_matches", "emit", "using", "Trace"]
