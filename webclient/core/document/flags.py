"""FlagsBacking: the ``flags`` facet -- the conclusions a caller (and the ``auto``
ladder / the onboarding pipeline) act on, each built from tiered, confidence-scored
:class:`Signal` evidence.

Flags:
  * ``spa`` -- the page is client-rendered (browser remedy).
  * ``anti_bot_present`` / ``anti_bot_triggered`` -- a bot-management vendor is in
    front of the site / is challenging us right now.
  * ``login_present`` / ``login_required`` -- a login form exists / a wall blocks the
    content.
  * ``pagination`` / ``forms`` / ``buttons`` -- the interactive shape of the page.

The request/static signals come from the pure :mod:`webclient.resiliency.detect`
(so a remote resolve agrees); this facet adds the rendered/network signals it can
read from the captured DOM/network events (a real SPA's injected content + its XHR
endpoints) and the tree-based signals (pagination / forms / buttons). A total facet:
it always applies; a flag with no evidence is simply ``present=False``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

from ...models import DOMUpdateEvent, NetworkEvent
from ...resiliency.detect import _framework, build_flag, static_flags
from ..web_core import Backing
from .html import _norm, tree
from .models import Flag, Form, Signal, Stage, XhrCall

if TYPE_CHECKING:
    from . import Document

_SPA_RATIO = 0.4  # injected-text share that alone marks a SPA
_SPA_MAIN_RATIO = 0.15  # lower bar when the injection is main-area + same-origin XHR


class FlagsBacking(Backing):
    """The ``flags`` facet -- per-flag ops returning a :class:`Flag`, plus a
    ``flags()`` digest and the ``framework``/``xhr_endpoints`` data ops."""

    provides = frozenset(
        {"flags", "spa", "anti_bot_present", "anti_bot_triggered", "login_present",
         "login_required", "pagination", "forms", "buttons", "framework", "xhr_endpoints"}
    )
    gate = "ok"

    def applies(self, core: "Document") -> bool:
        return True

    # -- request/static flags (from the pure detector) ------------------------
    def _static(self, core: "Document") -> "dict[str, Flag]":
        chain = [core.url, core.final_url] if core.final_url and core.final_url != core.url else [core.url]
        return static_flags(
            core.status_code, core.response_headers, core._set_cookies, core.content, chain
        )

    def anti_bot_present(self, core: "Document") -> Flag:
        """A bot-management vendor is in front of the site (a fingerprint), whether or
        not it is challenging us now."""
        return self._static(core)["anti_bot_present"]

    def anti_bot_triggered(self, core: "Document") -> Flag:
        """The site is actively challenging/blocking this request -- remedy ``stealth``
        for a named vendor, ``proxy`` for a bare block."""
        return self._static(core)["anti_bot_triggered"]

    def login_present(self, core: "Document") -> Flag:
        """A login form exists on the page (not necessarily a wall)."""
        return self._static(core)["login_present"]

    def login_required(self, core: "Document") -> Flag:
        """A login wall blocks the content (no transport remedy -- needs credentials)."""
        return self._static(core)["login_required"]

    # -- spa: static + rendered + network evidence ----------------------------
    def spa(self, core: "Document") -> Flag:
        """The page builds its content client-side. Static evidence (framework marker /
        empty shell) is joined with rendered evidence (``body_injected``) and network
        evidence (``xhr_composed``); ``value`` is the same-origin XHR endpoints so a
        caller can hit the data API instead of scraping the rendered SPA."""
        static = self._static(core)["spa"]
        rendered = self._spa_rendered(core)
        endpoints = [e.url for e in self.xhr_endpoints(core)]
        return build_flag(
            "spa", [*static.signals, *rendered], remedy="browser", value=endpoints or None
        )

    def _spa_rendered(self, core: "Document") -> "list[Signal]":
        ratio, in_main, same_origin_xhr = self._injection(core)
        out: list[Signal] = []
        if ratio >= _SPA_RATIO:
            out.append(Signal(
                name="body_injected", flag="spa", stage="rendered", confidence=0.9,
                reason=f"{ratio:.0%} of the page's text was injected after the initial response",
                value=ratio,
            ))
        if in_main and same_origin_xhr >= 1 and ratio >= _SPA_MAIN_RATIO:
            out.append(Signal(
                name="xhr_composed", flag="spa", stage="network", confidence=0.95,
                reason=f"main content composed from {same_origin_xhr} same-origin XHR call(s)",
                value=same_origin_xhr,
            ))
        return out

    # -- tree-based flags (html/xml only) -------------------------------------
    def pagination(self, core: "Document") -> Flag:
        """The dataset spans multiple pages. ``value`` is a short note on the pattern
        (a rel=next link, a page-param, a pagination widget)."""
        root = self._root(core)
        if root is None:
            return build_flag("pagination", [])
        base = core.final_url or core.url
        sigs: list[Signal] = []
        if root.cssselect('a[rel="next"], link[rel="next"]'):
            sigs.append(self._s("rel_next_link", "pagination", "static", 0.9, "a rel=next link"))
        if root.cssselect('.pagination, [class*="pagination"], [class*="pager"], [aria-label*="agination"]'):
            sigs.append(self._s("pagination_ui", "pagination", "static", 0.6, "a pagination widget"))
        param = next(
            (h for el in root.cssselect("a[href]")
             if (h := el.get("href")) and any(p in h for p in ("?page=", "&page=", "?p=", "&p=", "/page/"))),
            None,
        )
        if param is not None:
            sigs.append(self._s("page_param_links", "pagination", "static", 0.5,
                                 "links with a page parameter", urljoin(base, param)))
        nums = [t for el in root.cssselect("a[href]") if (t := _norm("".join(el.itertext()))).isdigit()]
        if len(nums) >= 3:
            sigs.append(self._s("numbered_sequence", "pagination", "static", 0.6,
                                 "a numbered page sequence"))
        return build_flag("pagination", sigs, value=(sigs[0].value if sigs else None))

    def forms(self, core: "Document") -> Flag:
        """Interactive forms on the page. ``value`` is the form list (method / action /
        field names) so a caller can drive them."""
        root = self._root(core)
        if root is None:
            return build_flag("forms", [])
        base = core.final_url or core.url
        forms = [
            Form(
                method=(el.get("method") or "get").lower(),
                action=urljoin(base, el.get("action")) if el.get("action") else None,
                field_names=sorted({n for f in el.cssselect("input, select, textarea") if (n := f.get("name"))}),
            )
            for el in root.cssselect("form")
        ]
        sigs = ([self._s("form_element", "forms", "static", 0.9, f"{len(forms)} form(s)")] if forms else [])
        return build_flag("forms", sigs, value=forms or None)

    def buttons(self, core: "Document") -> Flag:
        """Interactive buttons on the page. ``value`` is a sample of button labels."""
        root = self._root(core)
        if root is None:
            return build_flag("buttons", [])
        labels: list[str] = []
        sigs: list[Signal] = []
        btns = root.cssselect('button, input[type="submit"], input[type="button"]')
        if btns:
            labels = [t for el in btns if (t := _norm("".join(el.itertext())) or el.get("value") or "")][:10]
            sigs.append(self._s("button_element", "buttons", "static", 0.9, f"{len(btns)} button(s)"))
        if root.cssselect('[role="button"]'):
            sigs.append(self._s("role_button", "buttons", "static", 0.6, "role=button elements"))
        if root.cssselect("[onclick]"):
            sigs.append(self._s("onclick_attr", "buttons", "static", 0.4, "onclick handlers"))
        return build_flag("buttons", sigs, value=labels or None)

    # -- the digest -----------------------------------------------------------
    def flags(self, core: "Document") -> "list[Flag]":
        """Every flag that is present, most-actionable first -- the compact digest of
        "what is notable about this page"."""
        ops = (
            self.anti_bot_triggered, self.login_required, self.spa, self.anti_bot_present,
            self.login_present, self.pagination, self.forms, self.buttons,
        )
        return [f for op in ops if (f := op(core)).present]

    # -- data ops -------------------------------------------------------------
    def framework(self, core: "Document") -> "str | None":
        """The detected JS framework name (from markers in the served HTML), or None."""
        return _framework((core.content or b"").decode(core.encoding or "utf-8", "replace"))

    def xhr_endpoints(self, core: "Document") -> "list[XhrCall]":
        """The XHR/fetch calls a browser render captured -- an SPA's real data sources."""
        return [
            XhrCall(
                method=str(e.request.method).upper() if e.request is not None else "GET",
                url=str(e.request.dispatch("url")) if e.request is not None else "",
            )
            for e in core._events
            if isinstance(e, NetworkEvent) and e.resource_type in ("xhr", "fetch")
        ]

    # -- helpers --------------------------------------------------------------
    def _root(self, core: "Document") -> Any:
        """The parsed lxml tree for an html/xml document, or ``None`` (a treeless kind
        or a parse failure) so the tree-based flags return empty."""
        if core.kind not in ("html", "xml"):
            return None
        try:
            return tree(core)
        except Exception:
            return None

    def _s(self, name: str, flag: str, stage: Stage, conf: float, reason: str, value: Any = None) -> Signal:
        return Signal(name=name, flag=flag, stage=stage, confidence=conf, reason=reason, value=value)

    def _injection(self, core: "Document") -> "tuple[float, bool, int]":
        """``(injected_ratio, injected_in_main, same_origin_xhr_count)`` from captured
        events. ``injected_ratio`` = NET text grown past the DOMContentLoaded baseline
        / final text. All zero on a static fetch."""
        mutations = [e for e in core._events if isinstance(e, DOMUpdateEvent)]
        xhr = [e for e in core._events if isinstance(e, NetworkEvent) and e.resource_type in ("xhr", "fetch")]
        page_host = (urlparse(core.final_url or core.url).hostname or "").lower()
        same_origin_xhr = sum(
            1 for e in xhr
            if e.request is not None
            and (urlparse(str(e.request.dispatch("url"))).hostname or "").lower() == page_host
        )
        load_added = [e for e in mutations if e.kind == "added" and (e.detail or {}).get("phase") == "load"]
        injected_in_main = any((e.detail or {}).get("inMain") for e in load_added)
        stats = core._render_stats or {}
        total_text = int(stats.get("text", 0)) or len(_norm((core.content or b"").decode("utf-8", "replace")))
        if stats.get("dclText") is not None and total_text:
            net = max(0, total_text - int(stats["dclText"]))
            ratio = round(net / total_text, 3)
        else:
            ratio = 0.0
        return ratio, injected_in_main, same_origin_xhr


__all__ = ["FlagsBacking"]
