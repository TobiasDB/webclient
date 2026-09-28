"""FlagsBacking: the ``flags`` facet -- the typed Document surface over the detection
registry in :mod:`webclient.signals`.

This backing is thin on purpose: it builds a :class:`~webclient.signals.Context` from
the document (its response + the captured browser tree/events) and asks the registry
for the flags. All detection logic -- which signals feed which flag, at which stage,
with what confidence -- lives in :mod:`webclient.signals` (registry-driven and
extensible). The per-flag ops here are just the typed accessors the generated surface
needs; adding a flag to the SURFACE is one op + a regen, but the detection itself is
extended by registering a detector, no change here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from ...signals import Context, flags as detect_flags, framework as detect_framework
from ...signals import dom as _dom  # noqa: F401  (registers the rendered/tree detectors)
from ...signals import patterns as _patterns  # noqa: F401  (registers the pattern detectors)
from ...signals import cookies as _cookies  # noqa: F401  (registers the cookie_banner detectors)
from ..web_core import Backing
from .html import tree
from .models import DatasetHint, Filtering, Flag, NetworkView, Ordering, PaginationHint, PatternHint, XhrCall

if TYPE_CHECKING:
    from . import Document


class FlagsBacking(Backing):
    """The ``flags`` facet: per-flag ops returning a :class:`Flag` (spa /
    anti_bot_present / anti_bot_triggered / login_present / login_required /
    pagination / forms / buttons), plus a ``flags()`` digest and the
    ``framework``/``xhr_endpoints`` data ops. A total facet (always applies)."""

    provides = frozenset(
        {"flags", "spa", "anti_bot_present", "anti_bot_triggered", "login_present",
         "login_required", "pagination", "ordered", "filtered", "live", "tabbed", "forms",
         "buttons", "shadow_dom", "iframe", "cookie_banner", "large_document", "framework", "xhr_endpoints",
         "record_regions", "repeated_controls", "page_template", "patterns", "dataset", "network"}
    )
    gate = "ok"

    def applies(self, core: "Document") -> bool:
        """Always in play -- the flags facet is available on every document."""
        return True

    # -- the detection context + the flag set ---------------------------------
    def _context(self, core: "Document") -> Context:
        """A detection Context from the document: its response plus, for an html/xml
        document, the parsed tree and any captured browser DOM/network events."""
        root = None
        if core.kind in ("html", "xml"):
            try:
                root = tree(core)
            except Exception:
                root = None
        chain = [core.url, core.final_url] if core.final_url and core.final_url != core.url else [core.url]
        return Context.from_response(
            core.status_code, core.response_headers, core._set_cookies, core.content, chain,
            url=core.url, final_url=core.final_url or core.url,
            tree=root, events=core._events, render_stats=core._render_stats,
        )

    def _flags(self, core: "Document") -> "dict[str, Flag]":
        """The full flag set from the signal registry, keyed by flag name (the shared source
        every per-flag accessor reads). Memoised on the document so reading several flags is ONE
        detection pass, not one per accessor; the cache is cleared when the content changes
        (``drain`` after a live interaction), and a static document never changes."""
        cached = core._flag_cache
        if cached is None:
            cached = core._flag_cache = detect_flags(self._context(core))
        return cast("dict[str, Flag]", cached)

    # -- per-flag typed accessors ---------------------------------------------
    def spa(self, core: "Document") -> Flag:
        """The page builds its content client-side. ``value`` is the same-origin XHR
        endpoints -- an agent can hit the data API instead of scraping the SPA."""
        return self._flags(core)["spa"]

    def anti_bot_present(self, core: "Document") -> Flag:
        """A bot-management vendor is in front of the site (a fingerprint), whether or
        not it is challenging us now. ``value`` is the vendor name."""
        return self._flags(core)["anti_bot_present"]

    def anti_bot_triggered(self, core: "Document") -> Flag:
        """The site is actively challenging/blocking this request -- ``remedy``
        ``stealth`` for a named vendor, ``proxy`` for a bare block."""
        return self._flags(core)["anti_bot_triggered"]

    def login_present(self, core: "Document") -> Flag:
        """A login form exists on the page (not necessarily a wall)."""
        return self._flags(core)["login_present"]

    def login_required(self, core: "Document") -> Flag:
        """A login wall blocks the content (no transport remedy -- needs credentials)."""
        return self._flags(core)["login_required"]

    def pagination(self, core: "Document") -> Flag:
        """The dataset spans multiple pages. ``value`` is a :class:`PaginationHint`: the ways it could be
        paged (``modes``, best first -- each with the ``.paginate(...)`` to write and its evidence) and
        the totals a caption / header reveals. A hint only: nothing is walked."""
        return self._flags(core)["pagination"]

    def dataset(self, core: "Document") -> DatasetHint:
        """WHAT THIS LISTING IS, in one place, for deciding how to query it: is it paginated (and how),
        filtered (a subset), ordered (by what) -- and copyable plans (``recipes``) for the whole dataset,
        just the newest, without its filters and with one. ``summary`` says it in a few lines."""
        f = self._flags(core)
        pag = f["pagination"].value if f["pagination"].present else None
        filt = f["filtered"].value if f["filtered"].present else None
        order = f["ordered"].value if f["ordered"].present else None
        return dataset_hint(
            core.final_url or core.url,
            pag if isinstance(pag, PaginationHint) else None,
            filt if isinstance(filt, Filtering) else None,
            order if isinstance(order, Ordering) else None,
        )

    def ordered(self, core: "Document") -> Flag:
        """How the listing is SORTED (an :class:`~webclient.core.document.models.Ordering` value):
        the sort key / direction, whether it is controllable, and the sort param. Decides whether
        an early pagination stop is sound -- newest-first dates make a recency ``until`` stop safe;
        relevance / unknown order means the walk must exhaust."""
        return self._flags(core)["ordered"]

    def filtered(self, core: "Document") -> Flag:
        """The listing is NARROWED by filters (a :class:`~webclient.core.document.models.Filtering`
        value: the active filter params + the facet controls). So it is a subset -- pagination must
        preserve the active filters, and the facets are the tool to partition past a result cap."""
        return self._flags(core)["filtered"]

    def live(self, core: "Document") -> Flag:
        """The listing CHANGES over time (a :class:`~webclient.core.document.models.Liveness` value:
        the newest record date, whether it is recent, and the drift risk). A newest-first live list
        shifts while you page, so a cross-page ``key=`` dedup or a cursor is wanted."""
        return self._flags(core)["live"]

    def _pattern_flags(self, core: "Document") -> "dict[str, Flag]":
        """The STRUCTURAL ``"pattern"`` group of flags (record_regions / repeated_controls /
        page_template), memoised on the document -- kept out of the conclusion set (they are
        always-present detail), so a fresh ``group="pattern"`` build feeds the pattern reads."""
        cached = core._pattern_flag_cache
        if cached is None:
            cached = core._pattern_flag_cache = detect_flags(self._context(core), group="pattern")
        return cast("dict[str, Flag]", cached)

    def record_regions(self, core: "Document") -> Flag:
        """The repeating dataset regions to extract (value: a list of
        :class:`~webclient.core.document.models.PatternHint`, each a ``select_all`` target)."""
        return self._pattern_flags(core)["record_regions"]

    def repeated_controls(self, core: "Document") -> Flag:
        """Interactive controls that repeat per record -- an "add to cart" per card, a "load more"
        per section (value: a list of :class:`PatternHint`, one durable selector + count each)."""
        return self._pattern_flags(core)["repeated_controls"]

    def page_template(self, core: "Document") -> Flag:
        """The page's template signature (value: a :class:`PatternHint` whose ``subject`` is the
        signature digest) -- same signature = same KIND of page, for crawl dedup / clustering."""
        return self._pattern_flags(core)["page_template"]

    def patterns(self, core: "Document", *, for_: "str | None" = None) -> "list[PatternHint]":
        """The page's recurring-structure hints, most confident first: the record list(s) to
        ``select_all`` (``for_="extract"``), the repeated controls to act on per item
        (``"interact"``), and the page-template signature (``"crawl"``). A for_-filtered read over
        the pattern flags -- patterns are Signals/Flags, not a parallel registry."""
        got = self._pattern_flags(core)
        kinds = (("record_regions", "extract"), ("repeated_controls", "interact"), ("page_template", "crawl"))
        out: list[PatternHint] = []
        for flagname, consumer in kinds:
            if for_ is not None and for_ != consumer:
                continue
            value = got[flagname].value
            if value:
                out.extend(value)
        out.sort(key=lambda h: h.confidence, reverse=True)
        return out

    def tabbed(self, core: "Document") -> Flag:
        """The page splits content across TABS on the SAME page (Upcoming vs Past, year tabs,
        categories) -- distinct from pagination (another page of the same list). A dataset can
        live across several tabs, and one tab may be the default/visible one."""
        return self._flags(core)["tabbed"]

    def forms(self, core: "Document") -> Flag:
        """Interactive forms on the page. ``value`` is the form list (method / action
        / field names)."""
        return self._flags(core)["forms"]

    def buttons(self, core: "Document") -> Flag:
        """Interactive buttons on the page. ``value`` is a sample of button labels."""
        return self._flags(core)["buttons"]

    def large_document(self, core: "Document") -> Flag:
        """The document is LARGE -- its decoded body is big enough that a skeleton/outline of it
        gets trimmed or collapsed to fit a token budget, so a query author sees a reduced view.
        A property of the CONTENT SIZE alone (independent of any skeleton); ``value`` is the size
        in characters."""
        return self._flags(core)["large_document"]

    def shadow_dom(self, core: "Document") -> Flag:
        """The page hides content in shadow DOM -- invisible to a plain HTML snapshot.
        A browser render inlines each shadow root into the light DOM so the content is
        captured; ``value`` is how many roots were inlined."""
        return self._flags(core)["shadow_dom"]

    def iframe(self, core: "Document") -> Flag:
        """The page embeds content in iframe(s). A browser render inlines each SAME-ORIGIN
        frame's body into the parent DOM so its content is captured; ``value`` is the frame
        count. Cross-origin frames cannot be inlined (their content stays out of reach)."""
        return self._flags(core)["iframe"]

    def cookie_banner(self, core: "Document") -> Flag:
        """A cookie / consent banner covers the page. Static: the consent platform (OneTrust,
        Cookiebot, Didomi, …) or a cookie notice in the served HTML. A browser render answers it
        before the snapshot (the ``wc.cookies`` page script: reject / necessary-only first, else
        accept; hidden when nothing answers); ``value`` is what it did (vendor / action / button)."""
        return self._flags(core)["cookie_banner"]

    # -- the digest + data ops ------------------------------------------------
    def flags(self, core: "Document") -> "list[Flag]":
        """Every flag that is present, most-actionable first -- the compact digest of
        "what is notable about this page"."""
        order = (
            "anti_bot_triggered", "login_required", "spa", "data_api", "structured_data", "shadow_dom", "iframe",
            "anti_bot_present", "login_present", "cookie_banner", "pagination", "tabbed", "forms", "buttons",
        )
        got = self._flags(core)
        return [got[name] for name in order if got[name].present]

    def framework(self, core: "Document") -> "str | None":
        """The detected JS framework name (from markers in the served HTML), or None."""
        return detect_framework((core.content or b"").decode(core.encoding or "utf-8", "replace"))

    def network(self, core: "Document") -> NetworkView:
        """The page's NETWORK joined to the DOM it built: every request a browser load made (in
        start order: when, how long, from which frame, how big, its status and type) and, for each
        data request (xhr / fetch), the page nodes it most likely produced (CSS paths + their text,
        with a confidence) and its body. How a page got its data: read an SPA's API off it instead
        of its rendered HTML. A static document has none (its HTML is the whole story)."""
        from .network import network_view

        return network_view(core)

    def xhr_endpoints(self, core: "Document") -> "list[XhrCall]":
        """The XHR/fetch calls a browser render captured -- an SPA's real data sources."""
        from ...models import NetworkEvent

        return [
            XhrCall(
                method=str(e.request.method).upper() if e.request is not None else "GET",
                url=str(e.request.dispatch("url")) if e.request is not None else "",
            )
            for e in core._events
            if isinstance(e, NetworkEvent) and e.resource_type in ("xhr", "fetch")
        ]


__all__ = ["FlagsBacking"]


def dataset_hint(
    url: str, pag: "PaginationHint | None", filt: "Filtering | None", order: "Ordering | None"
) -> DatasetHint:
    """The :class:`DatasetHint` for a listing at ``url`` from its three flags' values: the facts, the
    recipes (plans an LLM can copy -- ``<...>`` marks what it must fill in) and a prompt summary."""
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

    import json

    q = json.dumps
    base = f"wq.reference({q(url)}).resolve()"
    best = pag.best if pag is not None else None
    pager = best.code if best is not None else ""
    recipes: dict[str, str] = {"all": base + pager}
    newest_first = order is not None and order.key == "date" and order.direction == "desc"
    if newest_first or best is None:
        recipes["latest"] = base  # newest-first: page one IS the latest
    else:
        until = ', until=wq.doc.select("<record date>").attr("text") < "<since>")'
        recipes["latest"] = base + (pager[:-1] + until if pager.endswith(")") else pager)
    if filt is not None and filt.active:
        u = urlparse(url)
        kept = [(k, v) for k, v in parse_qsl(u.query) if k not in filt.active]
        recipes["unfiltered"] = f"wq.reference({q(urlunparse(u._replace(query=urlencode(kept))))}).resolve()" + pager
    if filt is not None and filt.controls:
        recipes["filtered"] = f'wq.reference({q(url)}).with_params({filt.controls[0]}="<value>").resolve()' + pager
    lines = [f"listing: {url}"]
    if best is not None and pag is not None:
        size = ", ".join(x for x in (
            f"{pag.total_pages} pages" if pag.total_pages else "", f"{pag.total_items} items" if pag.total_items else "",
            f"{pag.page_size} per page" if pag.page_size else "") if x)
        lines.append(f"paginated ({best.evidence}{'; ' + size if size else ''}): {best.code}")
        lines += [f"  or: {m.code}  ({m.evidence})" for m in pag.modes[1:3]]
    else:
        lines.append("not paginated (one page)")
    if filt is not None and (filt.active or filt.controls):
        act = ", ".join(f"{k}={v}" for k, v in filt.active.items())
        lines.append(f"filtered{': ' + act if act else ''} -- a SUBSET" + (f"; filters: {', '.join(filt.controls[:5])}" if filt.controls else ""))
    else:
        lines.append("not filtered")
    if order is not None and order.key != "unknown":
        lines.append(f"ordered by {order.key} {order.direction}" + (f" (set with ?{order.param}=)" if order.param else ""))
    else:
        lines.append("order unknown -- walk the whole dataset; an early stop is not safe")
    return DatasetHint(
        url=url, paginated=pag, filtered=filt, ordered=order, recipes=recipes, summary="\n".join(lines),
    )
