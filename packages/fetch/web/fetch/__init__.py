"""web.fetch -- the transport layer: ``Request -> Snapshot``.

Perform a request and report the raw response as a :class:`Snapshot` (request + bytes +
transport metadata + any captured events). It does transport ONLY -- it never sniffs the
content kind or decodes a charset (that is web.parse), and it never raises for a transport
failure (that becomes ``snapshot.error``). Usable in isolation, depending only on web.kernel
(and httpx):

    from web.fetch import Request, HttpFetcher
    snap = await HttpFetcher().fetch(Request(url="https://example.com"))

The :class:`Fetcher` interface is the boundary every transport implements (static HTTP here;
a browser / HAR replay are further fetchers). A sync / lazy / remote face is the DSL's job,
not this layer's.
"""

from __future__ import annotations

from .base import Fetcher
from .browser import BrowserFetcher, BrowserSession
from .events import DOMEvent, FetchEvent, NetworkEvent
from .fingerprint import CHROME, Fingerprint
from .script import DOM_RECORDER, Script, ScriptRegistry
from .http import HttpFetcher, HttpSession
from .middleware import Handler, Middleware, stack
from .replay import Recorder, ReplayBackend
from .session import Session
from .pool import Pool
from .proxy import Proxy
from .request import Request
from .snapshot import Snapshot

__all__ = ["Request", "Snapshot", "Fetcher", "HttpFetcher", "BrowserFetcher", "BrowserSession", "HttpSession", "Session",
           "Script", "ScriptRegistry", "Proxy", "Pool", "Fingerprint", "CHROME", "DOM_RECORDER", "NetworkEvent", "DOMEvent",
           "FetchEvent", "Middleware", "Handler", "stack", "Recorder", "ReplayBackend"]
