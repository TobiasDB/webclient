"""02 · The cheapest transport that works -- a browser ONLY when the page needs one.

THE POINT (vs Playwright): Playwright launches a full browser for every page, always.
webclient resolves over plain HTTP first, DETECTS whether that was enough (was the content
injected by JS? is there an anti-bot / login wall?), and escalates to a browser render only
when the flags demand it. Same one call -- ``browser="auto"`` -- and the transport trail
shows exactly which tiers it took. Fast pages stay fast; JS pages still work.

(Needs chromium for the escalation on the JS-gated page.)
"""

from webclient import WebClient
from _site import serve, h1, kv


def main() -> None:
    base = serve()
    with WebClient() as wc:
        h1("A static page: HTTP is enough, no browser is launched")
        static = wc.fetch(f"{base}/", browser="auto")
        kv("tiers", static.transport().escalation)          # -> ['static']
        kv("cards", len(static.select_all(".card")))
        kv("spa flag", static.spa().present)

        h1("A JS-gated page: the same call auto-escalates to a browser")
        spa = wc.fetch(f"{base}/spa", browser="auto")
        kv("tiers", spa.transport().escalation)             # -> ['static', 'browser']: it escalated
        kv("records", len(spa.select_all("li.item")))       # content the static fetch couldn't see
        kv("why", "the static SPA flag fired first (demo 03) -> remedy 'browser' -> escalate")

    h1("Why it matters")
    kv("cost", "a browser is ~100x a HTTP GET; webclient pays it only when it must")
    kv("automatic", "no per-site config -- the flags decide, from real signals")
    kv("honest", "transport().escalation is an auditable record of what it took")
    kv("Playwright", "would launch chromium for BOTH pages, including the static one")


if __name__ == "__main__":
    main()
