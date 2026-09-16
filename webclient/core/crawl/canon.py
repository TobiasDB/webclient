"""URL canonicalization, scope, and link-scoring vocabulary for the crawl.

Pure, self-contained helpers (no cores, no IO): the dedup key (:func:`_canon`),
scope (:func:`_registrable`), the resource/pagination/locale predicates, and the
tuned constant tables the frontier's importance score reads. :mod:`.backing` (the
traversal ops) imports this vocabulary; keeping it here keeps the Backing about
*traversal*, not about URL string-surgery.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

# --------------------------------------------------------------------------- #
# Canonicalization / scope vocabulary
# --------------------------------------------------------------------------- #

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

#: a ``/page/N`` (or ``/pg/N``) pagination path segment.
_PAGE_PATH_RE = re.compile(r"/(?:page|pg)/(\d+)(?=/|$)", re.I)


def _path(url: str) -> str:
    return urlparse(url).path or ""


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
    small public-suffix heuristic keeps three labels for ``example.co.uk``. An IP
    literal is returned whole (never folded -- ``192.168.1.1`` and ``10.0.1.1`` are
    different machines, not a shared "1.1" domain)."""
    host = _fold_host(host)
    labels = host.split(".")
    if len(labels) <= 2 or all(label.isdigit() for label in labels):  # short host / IPv4
        return host
    keep = 3 if labels[-2] in _PUBLIC_SLDS else 2
    return ".".join(labels[-keep:])


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
        port = parts.port  # lazily parsed -- raises ValueError on a bad/huge port
    except ValueError:
        return url
    host = _dedup_host(urlparse(url).hostname or "")
    netloc = f"{host}:{port}" if port and port not in (80, 443) else host
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


def _ext(path: str) -> str:
    """The lowercased file extension of a URL path (``""`` if none)."""
    last = path.rsplit("/", 1)[-1]
    return last.rsplit(".", 1)[-1].lower() if "." in last else ""


def _url_entropy(url: str) -> float:
    """Shannon entropy (bits/char) of the URL path -- high for opaque / hashed /
    tracking URLs (a random-looking slug), low for readable ones. A scorer signal."""
    import math
    from collections import Counter

    path = _path(url)
    if not path:
        return 0.0
    n = len(path)
    return -sum((c / n) * math.log2(c / n) for c in Counter(path).values())


def _cctld(host: str) -> str | None:
    """The 2-letter country-code TLD of a host (``"uk"`` for ``bbc.co.uk``), or
    ``None`` for a gTLD / IP. Used by the crawl's country allow/deny filters."""
    labels = host.lower().split(".")
    tld = labels[-1] if labels else ""
    return tld if len(tld) == 2 and tld.isalpha() else None


# --------------------------------------------------------------------------- #
# Link-scoring vocabulary (the tuned constants the frontier's importance score
# reads; the score FUNCTION lives on the Backing, which has the receiver).
# --------------------------------------------------------------------------- #

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
        "twitter.com", "x.com", "facebook.com", "linkedin.com", "instagram.com",
        "youtube.com", "youtu.be", "pinterest.com", "reddit.com", "tiktok.com",
        "threads.net", "t.me", "whatsapp.com",
    }
)

#: editorial path segments (news / blog / story ...) -- a strong article signal.
_EDITORIAL = (
    "/news", "/blog", "/story", "/stories", "/article", "/press", "/post", "/posts",
    "/insight", "/newsroom",
)

#: a dated permalink segment (``/2026/09/...``) -- reads like an article URL.
_DATE_RE = re.compile(r"/(?:19|20)\d\d/")
