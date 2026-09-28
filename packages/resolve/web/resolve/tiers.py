"""The escalation ladder -- resolve POLICY, built from fetch's backends.

Fetch is just fetch: it offers configurable backends (http/browser, with proxy / fingerprint /
chrome channel) and each one only fetches. It does NOT know about tiers. THIS is where the tiers
are ordered into a ladder and (via :func:`~web.resolve.escalate`) climbed on a block signal.

The canonical ladder, cheapest / least-evasive first:

    http · http+fingerprint · [http+proxy] · browser · browser+fingerprint · [browser+proxy]
    · chrome · [chrome+proxy]

Each backend is lazy -- a browser/chrome tier launches nothing until a request actually climbs to
it -- so the full ladder is cheap to build and pay-as-you-go. The proxy tiers are included only
when a ``proxy`` is configured (a proxy tier with no proxy is meaningless).
"""

from __future__ import annotations

from web.fetch import BrowserFetcher, Fetcher, HttpFetcher


def ladder(*, proxy: str | None = None) -> list[Fetcher]:
    """The canonical transport ladder (see the module docstring). ``ladder()[0]`` is the base
    tier; the rest are escalation tiers a Resolver climbs on a block signal."""
    tiers: list[Fetcher] = [HttpFetcher(), HttpFetcher(fingerprint=True)]
    if proxy:
        tiers.append(HttpFetcher(proxy=proxy))
    tiers += [BrowserFetcher(), BrowserFetcher(fingerprint=True)]
    if proxy:
        tiers.append(BrowserFetcher(proxy=proxy))
    tiers.append(BrowserFetcher(channel="chrome"))
    if proxy:
        tiers.append(BrowserFetcher(channel="chrome", proxy=proxy))
    return tiers


__all__ = ["ladder"]
