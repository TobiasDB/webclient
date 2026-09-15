"""CrawlBacking: the site-traversal ops for a :class:`Crawl` core.

``step`` fetches one round of the frontier (the caller's selection, or -- in auto
mode -- the top-``width`` edges best-first by keyword relevance), summarises each
page, and expands the frontier with its in-scope, deduped, robots-allowed links.
``run`` auto-drives ``step`` to completion. Built ON the interface -- the owning
client's ``afetch`` for transport, the document's ``select_all``/``attr`` for link
discovery -- so a crawl is one backing over existing cores.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from ..web_core import Backing
from .models import Edge

if TYPE_CHECKING:
    from urllib.robotparser import RobotFileParser

    from . import Crawl

#: query params that never identify a distinct page (tracking / analytics); dropped
#: when canonicalising so ``/p?utm_source=x`` and ``/p`` are the same crawl target.
_TRACKING = frozenset(
    {"fbclid", "gclid", "gclsrc", "dclid", "msclkid", "mc_eid", "igshid"}
)  # unambiguous analytics params; ``ref``/``ref_src`` are left in (can be meaningful)

#: pagination query params -- a page-number (``page``/``pg``/…) or an offset
#: (``offset``/``start``/``skip``). These are dropped ENTIRELY from the dedup key, so
#: *every* page of one listing collapses to a single crawl target: once any page has
#: been seen, all the other pages of that series dedup (rather than flooding the
#: frontier with ``?page=2``, ``?page=3``, …). ``p`` is deliberately excluded -- it is
#: too often a post/id param (``?p=123``), not a page number.
_PAGE_NUM_PARAMS = frozenset({"page", "pg", "pagenum", "paged", "pagina", "pn"})
_OFFSET_PARAMS = frozenset({"offset", "start", "skip"})
_PAGINATION_PARAMS = _PAGE_NUM_PARAMS | _OFFSET_PARAMS

#: locale / language / country selector query params -- dropped for dedup so the
#: same page in different locales collapses to one crawl target.
_LOCALE_PARAMS = frozenset(
    {"locale", "lang", "language", "hl", "gl", "lr", "country", "region",
     "ui_locales", "setlang", "culture"}
)

#: ISO 639-1 language + ISO 3166-1 country codes that appear as URL locale segments
#: (``/en/…``, ``/en-us/…``, ``/jp/…``) or locale subdomains (``fr.site.com``). A
#: leading locale segment/subdomain is folded for dedup, so a page's many localised
#: copies collapse to one crawl target (e.g. a big multi-region site is crawled once,
#: not once per country).
_LOCALE_CODES = frozenset(
    (
        "aa ab ae af ak am an ar as av ay az ba be bg bh bi bm bn bo br bs ca ce ch co "
        "cr cs cu cv cy da de dv dz ee el en eo es et eu fa ff fi fj fo fr fy ga gd gl "
        "gn gu gv ha he hi ho hr ht hu hy hz ia id ie ig ii ik io is it iu ja jv ka kg "
        "ki kj kk kl km kn ko kr ks ku kv kw ky la lb lg li ln lo lt lu lv mg mh mi mk "
        "ml mn mr ms mt my na nb nd ne ng nl nn no nr nv ny oc oj om or os pa pi pl ps "
        "pt qu rm rn ro ru rw sa sc sd se sg si sk sl sm sn so sq sr ss st su sv sw ta "
        "te tg th ti tk tl tn to tr ts tt tw ty ug uk ur uz ve vi vo wa wo xh yi yo za "
        "zh zu "  # -- ISO 639-1 language codes above --
        "us gb ca au nz jp cn tw hk sg my ph vn pk bd lk sa il eg ng ke br cl pe mx ve "
        "ec uy py bo cr pa gt do at ch lu gr cz sk hu bg hr si rs ua by dk"  # countries
    ).split()
)

#: second-level labels that are really public suffixes (``example.co.uk``), so the
#: registrable domain keeps three labels there, not two.
_PUBLIC_SLDS = frozenset("co com org net gov edu ac mil gob gouv go or ne".split())

#: file extensions whose links are page *assets*, not crawlable documents -- an
#: anchor pointing at one is dropped from the frontier (it is a resource to load,
#: not a page to fetch and expand). XHR data-APIs (often ``.json``) are added by a
#: different path (``_expand_xhr``) so they are deliberately not listed here.
_RESOURCE_EXT = frozenset(
    "css js mjs map "
    "png jpg jpeg gif svg webp ico bmp avif tif tiff heic "
    "woff woff2 ttf eot otf "
    "mp4 webm mp3 wav ogg oga mov avi mkv m4a m4v flv "
    "zip gz tgz tar rar 7z bz2 dmg exe pkg deb rpm apk msi".split()
)

#: region weights (a link inherits the importance of the page landmark it sits in,
#: as reported by the document's ``region`` op): article/main content and nav links
#: are what a crawl wants; footer / sidebar (legal, social, "more from us") links
#: are noise, so they sink.
_REGION_WEIGHT = {
    "article": 1.2,
    "main": 1.0,
    "nav": 0.8,
    "header": 0.2,
    "aside": -0.6,
    "footer": -1.2,
}

#: how much one keyword hit outweighs the importance heuristic. ``_link_score``
#: spans roughly -4..+4, so a single explicit keyword match (>= this) dominates it
#: -- a keyword-directed crawl surfaces the matching page first, with importance
#: only breaking ties among equally-matching links.
_KEYWORD_WEIGHT = 10.0

#: call-to-action anchor text -- the "read more" / "continue reading" links the
#: user specifically wants surfaced (a strong article signal).
_CTA = (
    "read more",
    "read the",
    "continue reading",
    "learn more",
    "view more",
    "see more",
    "full story",
    "read full",
    "more from",
    "keep reading",
)

#: boilerplate anchor text -- legal + housekeeping links that are almost never
#: worth crawling; they sink in the frontier. Matched as whole words (so an
#: *article* titled "Cookies Guide" or "Terms of Endearment" is not mistaken for a
#: cookie/legal link).
_BOILER_RE = re.compile(
    r"\b(?:privacy|terms|cookie|legal|accessibility|gdpr|do not sell|sitemap"
    r"|trademark|copyright|imprint)\b"
)

#: boilerplate *paths* -- a terminal legal/housekeeping segment (``/privacy``,
#: ``/cookie-policy``, ``/terms-of-use`` ...). Anchored so ``/blog/cookies-guide``
#: (a real article) does not match ``/cookie``.
_BOILER_PATH_RE = re.compile(
    r"/(?:privacy|terms|legal|cookies?|accessibility|gdpr|copyright|trademark|imprint)"
    r"(?:-(?:policy|policies|notice|statement|preferences|settings|choices"
    r"|of-use|of-service|and-conditions))?/?$"
)

#: social / sharing hosts -- off-site widget links (share buttons, follow icons),
#: not site content.
_SOCIAL_HOSTS = frozenset(
    {
        "twitter.com",
        "x.com",
        "facebook.com",
        "linkedin.com",
        "instagram.com",
        "youtube.com",
        "youtu.be",
        "pinterest.com",
        "reddit.com",
        "tiktok.com",
        "threads.net",
        "t.me",
        "whatsapp.com",
    }
)

#: editorial path segments (news / blog / story ...) -- a strong article signal.
_EDITORIAL = (
    "/news",
    "/blog",
    "/story",
    "/stories",
    "/article",
    "/press",
    "/post",
    "/posts",
    "/insight",
    "/newsroom",
)

#: a dated permalink segment (``/2026/09/...``) -- reads like an article URL.
_DATE_RE = re.compile(r"/(?:19|20)\d\d/")


def _ext(path: str) -> str:
    """The lowercased file extension of a URL path (``""`` if none)."""
    last = path.rsplit("/", 1)[-1]
    return last.rsplit(".", 1)[-1].lower() if "." in last else ""


def _fold_host(host: str) -> str:
    """Lowercase a bare host and fold ``www.`` into the apex."""
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


def _canon_host(url: str) -> str:
    """The folded host of a URL, for scope comparison."""
    return _fold_host(urlparse(url).hostname or "")


def _is_locale(seg: str) -> bool:
    """Whether a path segment / subdomain label is a locale code -- a bare code
    (``en``, ``jp``) or a ``lang-country`` / ``lang_country`` pair (``en-us``)."""
    s = seg.lower()
    if s in _LOCALE_CODES:
        return True
    for sep in ("-", "_"):
        if sep in s:
            a, _, b = s.partition(sep)
            if a in _LOCALE_CODES and b in _LOCALE_CODES:
                return True
    return False


def _strip_locale_path(path: str) -> str:
    """Drop a leading locale segment (``/en/news`` -> ``/news``) so a page's
    localised copies dedup to one target."""
    segs = path.split("/")  # path starts "/", so segs[0] == ""
    if len(segs) > 1 and _is_locale(segs[1]):
        return "/" + "/".join(segs[2:])
    return path


def _dedup_host(host: str) -> str:
    """The host for dedup: ``www.`` folded and a leading locale subdomain dropped
    (``fr.site.com`` / ``en.site.com`` -> ``site.com``)."""
    host = _fold_host(host)
    labels = host.split(".")
    if len(labels) > 2 and _is_locale(labels[0]):
        host = ".".join(labels[1:])
    return host


def _registrable(host: str) -> str:
    """The registrable domain (eTLD+1) of a host, for scope: subdomains of the same
    site share it (``news.adobe.com`` / ``blog.adobe.com`` -> ``adobe.com``). A
    small public-suffix heuristic keeps three labels for ``example.co.uk``."""
    labels = _fold_host(host).split(".")
    if len(labels) <= 2:
        return ".".join(labels)
    keep = 3 if labels[-2] in _PUBLIC_SLDS else 2
    return ".".join(labels[-keep:])


#: a ``/page/N`` (or ``/pg/N``) pagination path segment.
_PAGE_PATH_RE = re.compile(r"/(?:page|pg)/(\d+)(?=/|$)", re.I)


def _strip_pagination_path(path: str) -> str:
    """Drop a ``/page/N`` / ``/pg/N`` pagination segment (any page) so every page of
    a path-paginated listing collapses to one dedup target."""
    return _PAGE_PATH_RE.sub("", path) or "/"


def _is_paginated(url: str) -> bool:
    """Whether a URL is a *later* page of a listing (page 2+, offset>0, or
    ``/page/N`` with N>1) -- lower-value than a fresh link, so it is scored down.
    (For dedup, every page collapses; this is only for the surviving edge's score.)"""
    for k, v in parse_qsl(urlparse(url).query):
        kl, vv = k.lower(), v.strip()
        if kl in _PAGE_NUM_PARAMS and vv not in ("", "1"):
            return True
        if kl in _OFFSET_PARAMS and vv not in ("", "0"):
            return True
    m = _PAGE_PATH_RE.search(urlparse(url).path)
    return bool(m and m.group(1) != "1")


def _canon(url: str) -> str:
    """A canonical dedup key: lowercased scheme+host (``www.`` + a locale subdomain
    folded, default port dropped), leading locale + ``/page/N`` path segments
    stripped, trailing slash normalised, tracking / locale / *pagination* params
    removed and the rest sorted, fragment stripped -- so ``/p``, ``/p/``,
    ``/en/p?utm=1``, and **every page of a listing** (``/p?page=1``, ``/p?page=7``,
    ``/p/page/3``) all collapse to ONE key: once any page is seen the rest dedup."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = _dedup_host(urlparse(url).hostname or "")
    netloc = f"{host}:{parts.port}" if parts.port and parts.port not in (80, 443) else host
    path = _strip_pagination_path(_strip_locale_path(parts.path or "/"))
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if (kl := k.lower()) not in _TRACKING
        and not kl.startswith("utm_")
        and kl not in _LOCALE_PARAMS
        and kl not in _PAGINATION_PARAMS  # every page collapses to the base
    ]
    query = urlencode(sorted(kept))
    return urlunsplit(((parts.scheme or "https").lower(), netloc, path, query, ""))


def _path(url: str) -> str:
    return urlparse(url).path or ""


class CrawlBacking(Backing):
    """The traversal ops: ``step`` (one round), ``run`` (auto to completion), and
    the ``done`` predicate. Both fetch, so they are IO ops (the interface bridges
    them onto the client's sync/async dispatcher)."""

    provides = frozenset({"step", "run"})
    props = frozenset({"done"})
    io = frozenset({"step", "run"})  # both fetch -- the interface bridges them
    gate = "ok"

    def done(self, core: "Crawl") -> bool:
        """Whether the crawl is finished: closed, the frontier is empty, or the
        page budget is spent."""
        return (
            core.status == "closed"
            or not core.frontier
            or len(core.pages) >= core.max_pages
        )

    async def aexit(self, core: "Crawl", *exc: Any) -> None:
        """Close the crawl when its ``with`` block exits."""
        core.status = "closed"

    async def step(
        self, core: "Crawl", select: "list[Edge] | list[str] | None" = None
    ) -> "Crawl":
        """Fetch one round. ``select`` (a subset of ``frontier`` -- edges or their
        URLs) chooses which edges to expand; ``None`` takes the top-``width`` edges
        best-first (by keyword relevance in auto mode, else shallowest-first). Each
        fetched page is summarised into ``pages`` and its links added to
        ``frontier``. Returns the crawl (so ``crawl.step()`` chains/reads)."""
        chosen = self._select(core, select)
        # only take (and remove from the frontier) what the page budget allows, so a
        # nearly-full budget doesn't silently discard the un-fetched chosen edges --
        # they stay in the frontier for the next step.
        room = max(0, core.max_pages - len(core.pages))
        to_fetch = chosen[:room]
        taken = {e.url for e in to_fetch}
        core.frontier = [e for e in core.frontier if e.url not in taken]
        for edge in to_fetch:
            if core.obey_robots and not await self._allowed(core, edge.url):
                continue
            doc = await core._client.afetch(
                core._client.ref(edge.url),
                optional=True,
                browser=core.browser,
                resolve=core.resolve,
            )
            if not doc.ok:
                continue
            if core.browser:  # captured the render + its XHR events; free the page
                await core._client._arelease(doc)  # (also lets select run in-memory)
            # the crawl decides which backings populate each page's summary;
            # ``core.facets`` already carries the (lean-by-default) selection.
            core.pages.append(doc.summary(*core.facets))
            if edge.depth < core.max_depth and doc.kind in ("html", "xml"):
                self._expand(core, doc, edge.depth + 1)
            if core.browser and edge.depth < core.max_depth:
                # a browser render observed the page's XHR/fetch calls -- add those
                # data-API endpoints to the frontier so the crawl covers them too.
                self._expand_xhr(core, doc, edge.depth + 1)
        return core

    async def run(self, core: "Crawl") -> "Crawl":
        """Auto-drive: ``step`` (top-``width`` best-first) each round until
        ``done``. Returns the finished crawl."""
        while not self.done(core):
            await self.step(core)
        return core

    # -- frontier selection + scoring ----------------------------------------
    def _select(self, core: "Crawl", select: Any) -> "list[Edge]":
        if select is not None:
            wanted = {s.url if isinstance(s, Edge) else str(s) for s in select}
            return [e for e in core.frontier if e.url in wanted]
        ranked = sorted(core.frontier, key=lambda e: self._score(core, e), reverse=True)
        return ranked[: core.width]

    def _score(self, core: "Crawl", edge: Edge) -> float:
        """Best-first relevance: the edge's discovery-time importance (nav / article
        / "read more" high, footer / legal / social low) plus any keyword hits in
        the anchor text + URL, minus a tiny depth penalty (ties break toward
        shallower pages). With no keywords it is importance-first."""
        base = edge.score - 0.01 * edge.depth
        if core.keywords:
            blob = f"{edge.text} {edge.url}".lower()
            hits = sum(blob.count(k) for k in core.keywords)
            base += _KEYWORD_WEIGHT * hits  # an explicit keyword match dominates
        return base

    def _link_score(self, text: str, url: str, region: str) -> float:
        """Discovery-time importance of a link: high for article / "read more" /
        nav links, low for footer / legal / social / icon links. Combines the
        anchor's region, its text quality, and URL shape into one score -- the
        frontier is sorted by it so the useful links surface first."""
        t = " ".join(text.split()).lower()
        path = _path(url).lower()
        score = _REGION_WEIGHT.get(region, 0.0)

        if not t:  # an icon / image link -- no text for an LLM to act on
            score -= 1.0
        else:
            words = len(t.split())
            if 1 <= words <= 12:  # a real label, not a stray paragraph link
                score += 0.3
            if any(c in t for c in _CTA):  # "read more" / "continue reading" ...
                score += 1.2
            if _BOILER_RE.search(t):  # whole-word legal/housekeeping text
                score -= 1.0

        if any(seg in path for seg in _EDITORIAL):
            score += 0.8
        if _DATE_RE.search(path):  # dated permalink -- an article URL shape
            score += 0.4
        last = path.rstrip("/").rsplit("/", 1)[-1]
        if "-" in last and len(last) > 8 and "." not in last:  # a content slug
            score += 0.5
        if _BOILER_PATH_RE.search(path):  # a terminal legal path segment
            score -= 1.2
        if _canon_host(url) in _SOCIAL_HOSTS:  # off-site share / follow widget
            score -= 1.5
        if path in ("", "/"):  # bare homepage link (nav "home", logo)
            score -= 0.2
        if _is_paginated(url):  # a later listing page -- low value, and there are many
            score -= 0.8
        return round(score, 3)

    def _add_edge(
        self, core: "Crawl", url: str, text: str, depth: int, score: float = 0.0
    ) -> None:
        """Add one discovered URL to the frontier if it is in scope, matches
        include/exclude, and its canonical form has not been seen (so URL variants
        -- trailing slash, tracking params, www -- are not re-fetched). ``score`` is
        the discovery-time importance kept on the edge (the frontier is sorted by
        it)."""
        url = url.split("#", 1)[0]
        if not url.startswith(("http://", "https://")):
            return
        key = _canon(url)
        if key in core._seen:
            return
        # scope: same registrable domain (eTLD+1), so subdomains of the same site
        # (news./blog./www.) are in scope but a different domain is not.
        if core.same_origin and _registrable(urlparse(url).hostname or "") != _registrable(
            core.scope
        ):
            return
        path = _path(url)
        if core.include is not None and core.include not in path:
            return
        if core.exclude is not None and core.exclude in path:
            return
        core._seen.add(key)
        core.frontier.append(Edge(url=url, text=text, depth=depth, score=score))

    def _expand(self, core: "Crawl", doc: Any, depth: int) -> None:
        """Add ``doc``'s anchor links to the frontier -- dropping links to page
        assets (images / scripts / media ...), and scoring each by importance
        (region + text + URL shape) so nav / article / "read more" links outrank
        footer / legal / social ones. Anchor text is kept for keyword scoring."""
        for a in doc.select_all("a[href]"):
            url = str(a.attr("href").url)
            if _ext(_path(url)) in _RESOURCE_EXT:  # a resource link, not a page
                continue
            text = (a.text_content or "").strip()
            region = a.region  # the document's landmark op (nav / main / footer ...)
            self._add_edge(core, url, text, depth, self._link_score(text, url, region))
        self._sort_frontier(core)

    def _sort_frontier(self, core: "Crawl") -> None:
        """Keep the frontier sorted by importance (score desc, then shallowest) so
        the links surfaced to the caller/LLM lead with the useful ones."""
        core.frontier.sort(key=lambda e: (-e.score, e.depth))

    def _expand_xhr(self, core: "Crawl", doc: Any, depth: int) -> None:
        """Add the data-API endpoints a browser render observed (the page's XHR /
        fetch calls) to the frontier -- so a crawl using the browser covers the
        JSON APIs behind the page, not only its anchor links."""
        from ...models import NetworkEvent

        for e in doc.events_of(NetworkEvent):
            if getattr(e, "resource_type", None) not in ("xhr", "fetch"):
                continue
            req = e.request
            url = str(req.dispatch("url")) if req is not None else ""
            if url:
                # a data-API endpoint -- valuable (it's the page's actual data), so
                # it rides mid-frontier rather than sinking with resource links.
                self._add_edge(core, url, "[xhr]", depth, 0.5)
        self._sort_frontier(core)

    # -- robots.txt (cached per host) ----------------------------------------
    async def _allowed(self, core: "Crawl", url: str) -> bool:
        host = _canon_host(url)
        if host not in core._robots:  # load this host's robots.txt once
            core._robots[host] = await self._load_robots(core, url)
        robots: "RobotFileParser | None" = core._robots[host]
        return robots is None or robots.can_fetch("*", url)

    async def _load_robots(self, core: "Crawl", sample_url: str) -> Any:
        from urllib.robotparser import RobotFileParser

        p = urlparse(sample_url)
        doc = await core._client.afetch(
            core._client.ref(f"{p.scheme}://{p.netloc}/robots.txt"),
            optional=True,
            resolve=core.resolve,
        )
        if not doc.ok or not doc.content:
            return None
        rp = RobotFileParser()
        rp.parse(doc.content.decode("utf-8", "replace").splitlines())
        return rp


__all__ = ["CrawlBacking"]
