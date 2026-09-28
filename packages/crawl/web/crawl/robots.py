"""``robots.txt`` -- politeness: honour a site's crawl rules and discover its sitemaps.

A minimal parser over the resolved ``/robots.txt``: it answers "may I fetch this path?" for our
user-agent (matching the most specific applicable group, longest-match Allow/Disallow) and exposes
the ``Sitemap:`` URLs the file advertises (a better seed source than guessing ``/sitemap.xml``).
Fetching robots is itself a resolve, so it runs over the same Resolver as the crawl.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from web.fetch import Request
from web.resolve import Resolver


@dataclass
class Robots:
    """Parsed robots rules for our agent: ``(allow, path)`` rules + advertised sitemap URLs."""

    rules: list[tuple[bool, str]] = field(default_factory=list)  # (is_allow, path_prefix)
    sitemaps: list[str] = field(default_factory=list)

    def allowed(self, url: str) -> bool:
        """Whether ``url``'s path is permitted -- the longest matching rule wins; a tie favours
        Allow; no match means allowed (robots defaults to permit)."""
        path = urlsplit(url).path or "/"
        best_len, best_allow = -1, True
        for is_allow, prefix in self.rules:
            if path.startswith(prefix) and len(prefix) >= best_len:
                if len(prefix) > best_len or is_allow:
                    best_len, best_allow = len(prefix), is_allow
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
