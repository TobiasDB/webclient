"""HtmlBacking: tree ops for html/xml (select/attr, incl. attr("text")) plus the
markdown / text / elements / links render front doors -- all html-only.

The backing owns STATE and wiring (the cached parse, the document's events, the
correlation substrate, sub-core creation); every pure tree algorithm lives in
:mod:`webclient.dom` (markdown / skeleton / select / landmarks / classes / regex)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, overload
from urllib.parse import urljoin

from ...dom import clean_href as _clean_href, decode_html
from ...dom import norm as _norm
from ...dom import parse_html, strip_wc_attrs as _strip_wc_attrs, tag as _tag, text_of
from ...dom.classes import is_noise_class as _is_noise_class  # noqa: F401  (back-compat re-export)
from ...dom.landmarks import landmark_of
from ...dom.markdown import HEADINGS as _HEADINGS, SKIP as _SKIP, to_markdown, to_text
from ...dom.regex import regex_extract as _regex_extract
from ...dom.select import find as _find_in
from ...dom.skeleton import skeleton as _skeleton
from ...query.collection import Field
from ..reference import Reference, from_url
from ..web_core import Backing
from .models import Element

if TYPE_CHECKING:
    from . import Document
    from .correlate import Correlation


def tree(core: "Document") -> Any:
    """The parsed lxml root for a document (an element sub-core is its own
    element; otherwise parse ``content`` once and cache it on the core). Shared
    by ``HtmlBacking`` and the summary facets.

    XML (``kind == "xml"``) is parsed with the XML parser (namespaces, tag case
    and CDATA preserved), not the HTML parser. Both are handed the raw *bytes*, so
    lxml honours an in-document ``<meta charset>`` / BOM / ``<?xml encoding?>``
    rather than a pre-decoded string (which would mojibake a non-UTF-8 page)."""
    if core._element is not None:
        return core._element
    if core._tree is None:
        raw = core.content or b""
        # XML is parsed from raw bytes (libxml2 honours the in-document ``<?xml encoding?>``,
        # preserving namespaces/CDATA/tag-case); HTML is decoded with the core's known charset
        # first (so a Python codec libxml2 would reject still works) then parsed. Both degrade a
        # blank body to an empty document rather than raising. See :func:`webclient.dom.parse_html`.
        core._tree = (
            parse_html(raw, xml=True)
            if core.kind == "xml"
            else parse_html(_html_text(core, raw))
        )
    return core._tree


def _html_text(core: "Document", raw: bytes) -> str:
    """Decode this document's HTML bytes to text using its known ``encoding`` (WHATWG
    precedence handled by :func:`webclient.dom.decode_html`)."""
    return decode_html(raw, core.encoding)


def _regex_field(value: Any, pattern: "str | None", group: "int | str | None") -> "Field[str]":
    """Wrap an extracted ``value`` as a ``Field``, applying an optional regex ``pattern``.
    A ``None`` value or a non-matching pattern is a LENIENT miss (an empty ``Field``) so it
    composes with ``extract``/``filter`` -- it never raises."""
    if value is not None and pattern is not None:
        value = _regex_extract(value if isinstance(value, str) else str(value), pattern, group)
    return Field(value) if value is not None else Field(None, ok=False)


def _xhr_endpoints(core: "Document") -> "list[str]":
    """The data-API URLs the page fetched (XHR/fetch), deduped in order -- read from
    the captured network events (only present on a browser-rendered document)."""
    from ...models import NetworkEvent

    out: list[str] = []
    seen: set[str] = set()
    for e in core._events:
        if isinstance(e, NetworkEvent) and getattr(e, "resource_type", None) in ("xhr", "fetch"):
            req = getattr(e, "request", None)
            url = str(req.dispatch("url")) if req is not None else ""
            if url and url not in seen:
                seen.add(url)
                out.append(url)
    return out


def _correlation(core: "Document") -> "Correlation | None":
    """Build the XHR->DOM :class:`Correlation` for this document from its captured events
    + phase stamps. ``None`` when there is nothing to correlate (a static document, or a
    browser render that issued no XHR).

    The correlator defaults to the cheap :class:`OrderingCorrelator` (unchanged behaviour). When
    response bodies were captured AND ``WEBCLIENT_CORRELATOR=content`` is set, the content-matching
    :class:`ContentCorrelator` refines the ordering candidates by value matching -- a strict,
    opt-in narrowing that never widens the temporal gate."""
    import os

    from ...models import DOMUpdateEvent, NetworkEvent
    from .correlate import ContentCorrelator, Correlator, OrderingCorrelator

    net = [e for e in core._events if isinstance(e, NetworkEvent) and e.index is not None]
    stamps = getattr(core, "_stamps", []) or []
    if not net and not stamps:
        return None
    dom = [
        DOMUpdateEvent(
            node_id=str(s.get("node") or ""),
            detail={
                "xhr_index": s.get("xhr", 0),
                "action": s.get("action", 0),
                "t_s": s.get("t"),
                "text": s.get("text", ""),  # node text snippet -- for content matching
            },
        )
        for s in stamps
    ]
    correlator: Correlator = OrderingCorrelator()
    if os.environ.get("WEBCLIENT_CORRELATOR", "").lower() == "content" and any(
        e.body for e in net
    ):
        correlator = ContentCorrelator()
    return correlator.correlate(net, dom)


def _html_elements(root: Any) -> list[Element]:
    """Flatten the tree into an ordered list of typed content ``Element``s (headings, text,
    links, …) with stable ids and their section context -- the structured element surface."""
    out: list[Element] = []
    counter = 0
    section: str | None = None

    def next_id() -> str:
        nonlocal counter
        counter += 1
        return f"e{counter}"

    def walk(el: Any) -> None:
        nonlocal section
        for child in el:
            tag = _tag(child)
            if tag in _SKIP:
                continue
            if tag in _HEADINGS:
                section = next_id()
                out.append(
                    Element(
                        id=section, type="title", text=_norm("".join(child.itertext()))
                    )
                )
            elif tag in ("p", "li"):
                out.append(
                    Element(
                        id=next_id(),
                        type="text" if tag == "p" else "list_item",
                        text=_norm("".join(child.itertext())),
                        parent_id=section,
                    )
                )
            elif tag == "pre":
                out.append(
                    Element(
                        id=next_id(),
                        type="code",
                        text=_norm("".join(child.itertext())),
                        parent_id=section,
                    )
                )
            elif tag == "img":
                out.append(
                    Element(
                        id=next_id(),
                        type="image",
                        text=child.get("alt", ""),
                        parent_id=section,
                        metadata={"src": child.get("src", "")},
                    )
                )
            else:
                walk(child)

    walk(root)
    return out


def _miss(parent: "Document", message: str, error: Any) -> "Document":
    """A missing selection: raise a structured ``SelectError`` under RAISE, else a
    not-ok sub-document. ``SelectError`` is both a ``WebException`` (so one
    ``except WebException`` covers fetch failures and misses alike) and a
    ``LookupError`` (back-compat)."""
    from ...errors import RAISE, current_policy, make, select_error

    if (error or current_policy()) is RAISE:
        raise select_error(message)
    sub = parent._sub(None)
    sub.error = parent._note_error(make("select.no_match", message), "select")
    return sub


class HtmlBacking(Backing):
    """Tree ops for html/xml. ``select``/``select_all`` yield element
    Documents; ``text_content`` reads the element's decoded text (all
    descendant text, tags stripped -- the DOM ``textContent``); ``attr`` reads a
    real HTML attribute; ``region`` reports which page landmark an element sits
    in."""

    provides = frozenset(
        {"select", "select_all", "attr", "render", "as_json",
         "markdown", "text", "html", "links", "elements", "skeleton"}
    )
    collections = frozenset({"select_all", "links"})  # return a Collection of cores
    props = frozenset({"title", "region"})
    gate = "tree"

    def region(self, core: "Document") -> str:
        """The page landmark this element sits in -- ``nav`` / ``main`` /
        ``article`` / ``header`` / ``footer`` / ``aside`` (or ``""`` if none) --
        found by walking its ancestors for the nearest landmark tag, ARIA
        ``role``, or class/id hint. A standard-web-semantics primitive: "is this
        link in the nav, the article, or the footer?" (a crawl scores links by
        it; an LLM can filter on it)."""
        return landmark_of(core._element)

    # -- named render front doors (typed sugar over ``render(format)``) --------
    def markdown(self, core: "Document", *, main_content_only: bool = False) -> str:
        """The page as markdown (``render("markdown")`` with a proper ``str`` type
        and no stringly-typed format arg)."""
        return self.render(core, "markdown", main_content_only=main_content_only)

    def text(self, core: "Document", *, main_content_only: bool = True) -> str:
        """The page's readable text (nav/chrome stripped by default)."""
        return self.render(core, "text", main_content_only=main_content_only)

    def html(self, core: "Document") -> str:
        """The raw decoded HTML source."""
        return self.render(core, "html")

    def links(self, core: "Document") -> "list[Reference]":
        """The page's outbound links as References."""
        return self.render(core, "links")

    def elements(self, core: "Document") -> "list[Element]":
        """The page as a flat list of typed content blocks."""
        return self.render(core, "elements")

    def as_json(self, core: "Document") -> "Document":
        """Reparse THIS element's text as a JSON document -- for data injected into the page
        as a JSON blob rather than as DOM (a ``<script type="application/json">`` island, a
        ``__NEXT_DATA__`` / ld+json blob, an inlined API payload). Select the script/element,
        then ``.as_json()`` to switch to the JSON ops and dotted-path into it
        (``.select("data.items")`` / ``.select_all("items")`` / ``.attr(key)``). Non-JSON or
        empty text yields a not-ok document, exactly like a missed select."""
        from . import Document

        if core._missing:
            return core._sub(None)
        el = core._element if core._element is not None else self._tree(core)
        raw = "".join(el.itertext())  # the element's RAW text (unnormalised -> valid JSON)
        doc = Document(
            url=core.url, final_url=core.final_url, kind="json",
            status_code=core.status_code, content=raw.encode("utf-8", "replace"),
        )
        doc.root = core.name or core.root
        doc._client = core._client
        doc._events = core._events
        return doc

    def skeleton(
        self,
        core: "Document",
        *,
        max_lines: int = 400,
        text_chars: int = 40,
        max_depth: int = 30,
        max_siblings: int = 200,
        legend: bool = True,
        collapse: bool = False,
        drop_chrome: bool = False,
        annotate_origin: bool = True,
        correlate: bool = True,
        mark_records: bool = True,
        mark_interactive: bool = True,
    ) -> str:
        """A token-lean DOM skeleton -- an indented HTML open-tag outline with the
        bloat (scripts/styles/svg/…) removed and leaf text hinted, every sibling
        shown faithfully. Keeps every id and semantic class so an LLM can write CSS
        selectors for the page cheaply (feed this instead of raw HTML, then use the
        selectors with ``select``/``select_all``/``extract``); high-entropy hashed build
        classes (``css-1a2b3c``/``AMTIxG_grid``) are dropped as noise. ``collapse=True``
        merges structurally-identical siblings to ``×N`` for very repetitive pages;
        ``drop_chrome=True`` omits nav/footer/sidebar landmarks so a huge page's records
        aren't buried under menus.

        Each ENRICHMENT is individually controllable (all default on; each simply produces
        nothing when its data is absent -- e.g. a static document has no XHR to correlate):
        ``annotate_origin`` marks client-injected nodes ``[xhr]``/``[js]`` against the pre-JS
        baseline + lists observed data APIs; ``correlate`` lists the XHR/action timeline and
        annotates ``← after req[n] act[m]``; ``mark_records`` flags the repeating dataset
        region (``← RECORD LIST · N · select_all(...)``); ``mark_interactive`` flags
        NON-obvious controls (a div/span made clickable via role/onclick/tabindex)
        ``← clickable``. (A human-readable label for any element is available as the reusable
        :func:`webclient.core.document.naming.name` primitive -- not repeated here, since the
        tag signature already surfaces aria-label / alt / title / placeholder.)"""
        from ...dom.records import region_marks as _region_marks

        static_html = core._static_html if annotate_origin else None
        xhr = _xhr_endpoints(core) if annotate_origin else None
        correlation = _correlation(core) if correlate else None
        tree = self._tree(core)
        return _skeleton(
            tree,
            max_lines=max_lines,
            text_chars=text_chars,
            max_depth=max_depth,
            max_siblings=max_siblings,
            legend=legend,
            collapse=collapse,
            drop_chrome=drop_chrome,
            static_html=static_html,
            xhr_endpoints=xhr,
            correlation=correlation,
            region_marks=_region_marks(tree) if mark_records else None,
            mark_interactive=mark_interactive,
        )

    def applies(self, core: "Document") -> bool:
        """In play for markup documents (html/xml)."""
        return core.kind in ("html", "xml")

    def title(self, core: "Document") -> str | None:
        """The page's ``<title>`` text (whitespace-normalised), or ``None`` when absent."""
        node = self._find(core, "title")
        return _norm("".join(node[0].itertext())) if node else None


    @overload
    def render(
        self, core: "Document", format: Literal["elements"]
    ) -> "list[Element]":  # noqa: E501
        """Render to the flat typed element list."""
        ...
    @overload
    def render(
        self, core: "Document", format: Literal["links"]
    ) -> "list[Reference]":  # noqa: E501
        """Render to the resolved link references."""
        ...
    @overload
    def render(self, core: "Document", format: str, **options: Any) -> str:
        """Render to a string format (html/markdown/text/skeleton)."""
        ...

    def render(self, core: "Document", format: str, **options: Any) -> Any:
        """Render the document (or a selected element) into a requested format -- ``html``
        (cleaned serialisation), ``links`` (resolved ``Reference``s), ``markdown``/``text``
        (optionally main-content-only), or ``elements`` (the flat typed element list)."""
        if format == "html":
            if core._element is not None:  # a selected element: serialise it on demand
                from lxml import html as _lh

                return _strip_wc_attrs(_lh.tostring(core._element, encoding="unicode"))
            # graceful on a bogus charset; strip our internal correlation stamps from output
            return _strip_wc_attrs(_html_text(core, core.content or b""))
        root = self._tree(core)
        if format == "links":
            base = core.final_url or core.url
            return [
                from_url(urljoin(base, href))
                for el in root.cssselect("a[href]")
                if (href := _clean_href(el.get("href")))
            ]
        if format == "markdown":
            return to_markdown(root, main_content_only=bool(options.get("main_content_only")))
        if format == "text":
            return to_text(root, main_content_only=bool(options.get("main_content_only", True)))
        if format == "elements":
            return _html_elements(root)
        if format == "skeleton":
            return self.skeleton(core, **options)
        from ...errors import render_error

        raise render_error(f"no html render format {format!r}")

    def _tree(self, core: "Document") -> Any:
        """The document's parsed lxml tree (cached), rooted at the selected element if any."""
        return tree(core)

    def _find(self, core: "Document", selector: str) -> list[Any]:
        """Resolve a CSS or XPath ``selector`` to the matching elements, scoped to this node
        (:func:`webclient.dom.select.find`; the XML case-insensitive fallback applies to an
        xml document)."""
        return _find_in(self._tree(core), selector, xml=core.kind == "xml")

    def select(
        self,
        core: "Document",
        selector: str,
        *,
        index: int = 0,
        optional: bool = False,
        error: Any = None,
    ) -> "Document":
        """The first element matching a CSS or XPath ``selector``, itself selectable.
        Loud on a miss (pass ``optional=True`` for a not-ok element instead); ``index``
        picks the n-th match."""
        from ...errors import RETURN

        els = self._find(core, selector)
        if not (-len(els) <= index < len(els)):
            return _miss(core, f"no match for {selector!r}", RETURN if optional else error)
        return core._sub(els[index])

    def select_all(
        self,
        core: "Document",
        selector: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> "list[Document]":
        """Every element matching ``selector`` (an empty match is still a collection),
        each selectable. ``limit`` / ``offset`` bound it. Pair with ``extract`` to shape
        one row per match."""
        els = self._find(core, selector)[offset:]
        if limit is not None:
            els = els[:limit]
        return [core._sub(el) for el in els]

    @overload  # link attrs narrow to a Reference (overlaps the str overload)
    def attr(
        self, core: "Document", name: Literal["href", "src", "action"]
    ) -> "Reference":  # type: ignore[overload-overlap]
        """A link attribute (href/src/action) as a resolvable ``Reference``."""
        ...
    @overload
    def attr(
        self, core: "Document", name: str, pattern: str | None = None, *,
        group: int | str | None = None, optional: bool = False, error: Any = None,
    ) -> "Field[str]":
        """Any other attribute (or a text/html pseudo-attr) as a ``Field``."""
        ...

    def attr(
        self, core: "Document", name: str, pattern: str | None = None, *,
        group: int | str | None = None, optional: bool = False, error: Any = None,
    ) -> Any:
        """The one element accessor -- give me ``name`` from this node. A real HTML
        attribute (``class``, ``data-id``, …) as a ``Field``; the link attrs
        ``href``/``src``/``action`` as a resolvable ``Reference``; the pseudo-attrs
        ``"text"`` (all of the element's text), ``"text:own"`` (only its DIRECT text,
        excluding child elements) and ``"html"`` (its markup).

        ``pattern`` extracts a substring by regex: the value is searched (not
        anchored), and the ``group`` (an index or a named group; default: group 1 when
        the pattern has groups, else the whole match) is returned. The text pseudo-attrs
        and a regex non-match are LENIENT (an empty ``Field``, never an error) -- only an
        absent REAL attribute raises by default (soften it with ``optional=True`` /
        ``error=``), since asking for a missing attribute is the true mistake."""
        if name in ("text", "text:own"):
            raw = None if core._missing else self._text(core, own=name == "text:own")
            return _regex_field(raw, pattern, group)
        if name == "html":
            raw = None if core._missing else self.render(core, "html")
            return _regex_field(raw, pattern, group)
        if core._missing:
            # honour the declared type: a link attr is a Reference even on a miss
            # (an empty, not-ok one whose ``.url`` is "" -- never a Field, so
            # ``select(..., optional=True).attr("href").url`` can't AttributeError).
            # A regex on a link attr, though, extracts a substring -> a (lenient) Field.
            if name in ("href", "src", "action"):
                return _regex_field(None, pattern, group) if pattern is not None else \
                    self._link_ref(core, "")
            return Field(None, ok=False)
        el = core._element
        value = el.get(name) if el is not None else None
        if name in ("href", "src", "action"):
            url = urljoin(core.final_url or core.url, _clean_href(value))
            # a regex on a link attr extracts from the URL STRING (e.g. an id in the path) ->
            # a Field, not a Reference; without a pattern it stays a resolvable Reference.
            if pattern is not None:
                return _regex_field(url, pattern, group)
            return self._link_ref(core, url)
        if value is None:  # absent attribute -> raise (structured) by default
            from ...errors import RAISE, current_policy, select_error

            if not optional and (error or current_policy()) is RAISE:
                raise select_error(f"no attribute {name!r}", code="select.no_attribute")
            return Field(None, ok=False)
        return _regex_field(value.strip() if isinstance(value, str) else value, pattern, group)

    def _link_ref(self, core: "Document", url: str) -> "Reference":
        """A resolvable Reference for a link attr's URL, inheriting the client so it resolves."""
        ref = from_url(url)
        ref._client = core._client
        return ref

    def _text(self, core: "Document", *, own: bool = False) -> "str | None":
        """The element's visible text, whitespace-normalised (``None`` on a miss).
        ``own`` restricts it to the node's DIRECT text (its own text + child tails),
        excluding descendant elements' text."""
        if core._missing:
            return None
        el = core._element if core._element is not None else self._tree(core)
        return text_of(el, own=own)


__all__ = ["HtmlBacking"]
