"""``robots.txt`` -- politeness: honour a site's crawl rules and discover its sitemaps.

A parser over the resolved ``/robots.txt``: it answers "may I fetch this path?" for our user-agent
(RFC 9309 semantics -- LONGEST-matching rule wins, a tie favours Allow, with ``*``/``$`` wildcards)
and exposes the ``Sitemap:`` URLs the file advertises (a better seed source than guessing
``/sitemap.xml``). Fetching robots is itself a resolve, so it runs over the same Resolver as the
crawl. NB we do NOT use ``urllib.robotparser``: it implements the OLD first-match precedence (so a
longer ``Allow`` cannot override an earlier ``Disallow``) and does not honour wildcards.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from web.fetch import Request
from web.resolve import Resolver


def _match_len(pattern: str, path: str) -> int:
    """If the robots ``pattern`` matches ``path``, its length (for longest-match precedence), else
    -1. ``*`` matches any run, a trailing ``$`` anchors the end (RFC 9309); otherwise it is a
    prefix match."""
    body = pattern[:-1] if pattern.endswith("$") else pattern
    regex = "^" + "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    if pattern.endswith("$"):
        regex += "$"
    return len(pattern) if re.match(regex, path) else -1


@dataclass
class Robots:
    """Parsed robots rules for our agent: ``(allow, path-pattern)`` rules + advertised sitemap URLs."""

    rules: list[tuple[bool, str]] = field(default_factory=list)  # (is_allow, path_pattern)
    sitemaps: list[str] = field(default_factory=list)

    def allowed(self, url: str) -> bool:
        """Whether ``url``'s path is permitted -- the longest matching rule wins; a tie favours
        Allow; no match means allowed (robots defaults to permit)."""
        parts = urlsplit(url)
        path = parts.path or "/"
        if parts.query:  # robots matches against path + query (so /*.pdf$ won't block /a.pdf?x=1)
            path += "?" + parts.query
        best_len, best_allow = -1, True
        for is_allow, pattern in self.rules:
            mlen = _match_len(pattern, path)
            if mlen >= 0 and mlen >= best_len and (mlen > best_len or is_allow):
                best_len, best_allow = mlen, is_allow
        return best_allow


def parse_robots(text: str, *, agent: str = "*") -> Robots:
    """Parse robots.txt text, keeping the rules for ``agent`` (falling back to the ``*`` group) and
    every ``Sitemap:`` line (which is global, not per-group)."""
    star: list[tuple[bool, str]] = []
    mine: list[tuple[bool, str]] = []
    sitemaps: list[str] = []
    active: "list[str]" = []  # which agent groups the current lines apply to
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "sitemap":
            sitemaps.append(value)
        elif key == "user-agent":
            active = [value.lower()]
        elif key in ("allow", "disallow") and value:
            rule = (key == "allow", value)
            if "*" in active:
                star.append(rule)
            if agent.lower() in active:
                mine.append(rule)
    return Robots(rules=mine or star, sitemaps=sitemaps)


async def robots(resolver: Resolver, base: str, *, agent: str = "*") -> Robots:
    """Fetch and parse ``/robots.txt`` for ``base``'s origin (an empty/failed fetch = no rules)."""
    parts = urlsplit(base)
    doc = await resolver.resolve(Request(url=urljoin(f"{parts.scheme}://{parts.netloc}", "/robots.txt")))
    return parse_robots(doc.text, agent=agent)


__all__ = ["Robots", "robots", "parse_robots"]
