"""M1 pure core: Reference, Node, Document and its typed views.

Implements the no-I/O part of the interface spec (/models.py at the repo
root). Methods owned by later milestones raise NotImplementedError naming
their milestone (ISSUES #12).

Error philosophy (ISSUES #8): loud by default, leniency opt-in via
``optional=True``.
"""
from __future__ import annotations

import json as _json
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Sequence, overload
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from pydantic import BaseModel, PrivateAttr
from typing_extensions import Self

# lxml / charset-normalizer are imported lazily (only where parsing happens),
# so importing the models -- and therefore the lazy layer and the remote
# client -- does not require the native lxml wheel (ISSUES #36).
if TYPE_CHECKING:
    from .live import LiveDocument

from .events import (
    ActionEvent,
    AssetEvent,
    E,
    Event,
    FetchEvent,
    NavigationEvent,
    NetworkEvent,
    Topic,
    XHREvent,
)

HttpMethod = Literal["get", "post", "put", "patch", "delete", "head", "options"]

DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}

_LINK_ATTRS = ("href", "src", "action")


class OnError(str, Enum):
    """Error policy for a lazy-pipeline step when its input is missing or it
    fails: drop the row, abort the pipeline, or keep the row with None."""

    skip = "skip"
    raise_error = "raise"
    ignore = "ignore"


class FetchError(Exception):
    """A fetch failed: transport error, or non-2xx status. Raised unless the
    fetch was made with ``optional=True`` (which instead returns the not-ok
    Document for inspection via ``.ok``). Carries ``document`` when a
    response was received."""

    def __init__(self, message: str, document: "Document | None" = None):
        super().__init__(message)
        self.document = document


class Proxy(BaseModel):
    url: str
    username: str | None = None
    password: str | None = None

    @property
    def authenticated_url(self) -> str:
        if self.username is None:
            return self.url
        scheme, _, rest = self.url.partition("://")
        auth = self.username + (f":{self.password}" if self.password else "")
        return f"{scheme}://{auth}@{rest}"


class Script(BaseModel):
    """JS injected into a browser page (M4)."""

    source: str
    run_at: Literal["init", "domcontentloaded", "load"] = "init"


class Element(BaseModel):
    """A typed content block -- the "elements" representation."""

    id: str = ""
    type: str = "text"               # title/text/list_item/table/code/image
    text: str = ""
    parent_id: str | None = None
    metadata: dict[str, Any] = {}


def _later(feature: str, milestone: str) -> NotImplementedError:
    return NotImplementedError(f"{feature} lands in {milestone}")


# --------------------------------------------------------------------------- #
# Selection engine (shared by Document and Node)
# --------------------------------------------------------------------------- #

def _is_xpath(selector: str) -> bool:
    return selector.startswith("/") or selector.startswith("./")


def _select_elements(root: Any, selector: str) -> list[Any]:
    """CSS or XPath (auto-detected) -> element list. Elements only: XPath
    producing attributes, text or scalars is rejected (ISSUES #10)."""
    from lxml import etree
    if _is_xpath(selector):
        results = root.xpath(selector)
        if not isinstance(results, list):  # count(), boolean(), string()
            raise ValueError(
                f"XPath {selector!r} produces a scalar; selection returns "
                "elements only -- use .attr() / .text for values")
        non_elements = [r for r in results if not isinstance(r, etree._Element)]
        if non_elements:
            raise ValueError(
                f"XPath {selector!r} selects non-element results; selection "
                "returns elements only -- use .attr() / .text for values")
        return results
    return root.cssselect(selector)


# --------------------------------------------------------------------------- #
# Reference
# --------------------------------------------------------------------------- #

class Reference(BaseModel):
    """A request spec: everything needed to (re)fetch a resource."""

    hostname: str
    method: HttpMethod = "get"
    scheme: str = "https"
    port: int | None = None          # None -> default port for scheme
    path: str = ""
    fragment: str = ""

    params: dict[str, str | list[str]] = {}
    headers: dict[str, str] = {}
    cookies: dict[str, str] = {}
    body: bytes | None = None
    json_body: Any | None = None
    form: dict[str, str] | None = None
    follow_redirects: bool = True
    timeout: float | None = None

    _client: Any = PrivateAttr(default=None)
    _session: Any = PrivateAttr(default=None)

    @classmethod
    def from_url(
        cls,
        url: str,
        method: HttpMethod = "get",
        params: dict[str, str | list[str]] | None = None,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> Self:
        parsed = urlparse(url)
        query: dict[str, str | list[str]] = {
            k: v[0] if len(v) == 1 else v for k, v in parse_qs(parsed.query).items()
        }
        if params:
            query.update(params)
        return cls(
            hostname=parsed.hostname or "",
            method=method,
            scheme=parsed.scheme or "https",
            port=parsed.port,
            path=parsed.path or "",
            fragment=parsed.fragment or "",
            params=query,
            headers=headers or {},
            cookies=cookies or {},
        )

    @property
    def url(self) -> str:
        """Reassembled absolute URL (default ports elided)."""
        port = ""
        if self.port is not None and self.port != DEFAULT_PORTS.get(self.scheme):
            port = f":{self.port}"
        url = f"{self.scheme}://{self.hostname}{port}{self.path}"
        if self.params:
            url += "?" + urlencode(self.params, doseq=True)
        if self.fragment:
            url += "#" + self.fragment
        return url

    # -- derivation helpers -------------------------------------------------
    def replace(self, **fields: Any) -> Self:
        return self.model_copy(update=fields)

    def with_params(self, **params: str) -> Self:
        return self.model_copy(update={"params": {**self.params, **params}})

    def join(self, href: str) -> Reference:
        return Reference.from_url(urljoin(self.url, href))

    # -- binding ------------------------------------------------------------
    def bind(self, client: Any, session: Any = None) -> Self:
        copy = self.model_copy()
        copy._client = client
        copy._session = session
        return copy

    @property
    def bound(self) -> Any:
        return self._client

    # -- fetching -----------------------------------------------------------
    @overload
    def fetch(self, *, optional: bool = ..., session: Any = ...,
              client: Any = ...) -> "Document": ...
    @overload
    def fetch(self, *, browser: Literal[True],
              scripts: Sequence[Script] | None = ...,
              wait_until: str = ..., optional: bool = ...,
              session: Any = ..., client: Any = ...) -> "LiveDocument": ...
    def fetch(self, *, browser: bool = False,
              scripts: Sequence[Script] | None = None,
              wait_until: str = "load",
              optional: bool = False,
              session: Any = None,
              client: Any = None) -> "Document | LiveDocument":
        """Fetch this reference. Raises FetchError on transport failure or
        non-2xx status unless ``optional=True``. Resolution: explicit
        ``client`` > bound client > process default."""
        from .client import default_client
        wc = client or self._client or default_client()
        sess = session or self._session
        if browser:
            return wc.fetch(self, browser=True, scripts=scripts,
                            wait_until=wait_until, optional=optional,
                            session=sess)
        return wc.fetch(self, optional=optional, session=sess)


# --------------------------------------------------------------------------- #
# Node
# --------------------------------------------------------------------------- #

class Node(BaseModel):
    """An element selected out of a Document."""

    _element: Any = PrivateAttr(default=None)
    _document: Any = PrivateAttr(default=None)

    @classmethod
    def _wrap(cls, element: Any, document: Document | None) -> Node:
        node = cls()
        node._element = element
        node._document = document
        return node

    @property
    def text(self) -> str:
        """Normalized text content (whitespace collapsed)."""
        return " ".join("".join(self._element.itertext()).split())

    @property
    def html(self) -> str:
        """Outer HTML of the element."""
        from lxml import etree
        return etree.tostring(self._element, encoding="unicode")

    @overload  # link-likes intentionally narrow str -> Reference
    def attr(self, name: Literal["href", "src", "action"]) -> Reference: ...  # type: ignore[overload-overlap]
    @overload
    def attr(  # type: ignore[overload-overlap]
        self, name: Literal["href", "src", "action"], *,
        optional: Literal[True]) -> Reference | None: ...
    @overload
    def attr(self, name: str) -> str: ...
    @overload
    def attr(self, name: str, *, optional: Literal[True]) -> str | None: ...
    def attr(self, name: str, *,
             optional: bool = False) -> Reference | str | None:
        value = self._element.get(name)
        if value is None:
            if optional:
                return None
            raise LookupError(f"no attribute {name!r} on <{self._element.tag}>")
        if name in _LINK_ATTRS:
            base: Reference | None = self._document
            return base.join(value) if base is not None else Reference.from_url(value)
        return value

    @overload
    def select(self, selector: str, *, index: int = 0) -> Node: ...
    @overload
    def select(self, selector: str, *, index: int = 0,
               optional: Literal[True]) -> Node | None: ...
    def select(self, selector: str, *, index: int = 0,
               optional: bool = False) -> Node | None:
        matches = _select_elements(self._element, selector)
        try:
            element = matches[index]
        except IndexError:
            if optional:
                return None
            raise LookupError(
                f"no match for {selector!r} at index {index} "
                f"({len(matches)} matches)") from None
        return Node._wrap(element, self._document)

    def select_all(self, selector: str, limit: int | None = None,
                   offset: int = 0) -> Sequence[Node]:
        matches = _select_elements(self._element, selector)
        matches = matches[offset:offset + limit if limit is not None else None]
        return [Node._wrap(e, self._document) for e in matches]

    @overload
    def events_of(self, event: type[E]) -> Sequence[E]: ...
    @overload
    def events_of(self, event: Topic) -> Sequence[Event]: ...
    def events_of(self, event: type[Event] | Topic) -> Sequence[Event]:
        # Static nodes are document-scoped; element narrowing needs the node
        # identity only live capture stamps (ISSUES #9).
        if self._document is None:
            return []
        return self._document.events_of(event)


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #

class Document(Reference):
    """A fetched resource. Extends Reference so it can be re-fetched."""

    id: str = ""
    session_id: str | None = None
    kind: Literal["html", "json", "xml", "binary"] = "html"
    content: bytes = b""
    status_code: int = 0
    response_headers: dict[str, str] = {}
    encoding: str | None = None
    final_url: str | None = None
    elapsed: float | None = None
    events: list[Event] = []

    _tree: Any = PrivateAttr(default=None)
    _views: dict[type, Any] = PrivateAttr(default_factory=dict)

    @property
    def text(self) -> str:
        """Body decoded via declared encoding, else detection, else utf-8."""
        if not self.content:
            return ""
        if self.encoding:
            try:
                return self.content.decode(self.encoding)
            except (LookupError, UnicodeDecodeError):
                pass
        from charset_normalizer import from_bytes as _detect_charset
        best = _detect_charset(self.content).best()
        if best is not None:
            return str(best)
        return self.content.decode("utf-8", errors="replace")

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def reload(self, *, optional: bool = False, client: Any = None) -> Any:
        """Re-fetch this document with its own request spec."""
        return self.fetch(optional=optional, client=client)

    # -- event store --------------------------------------------------------
    @overload
    def events_of(self, event: type[E]) -> Sequence[E]: ...
    @overload
    def events_of(self, event: Topic) -> Sequence[Event]: ...
    def events_of(self, event: type[Event] | Topic) -> Sequence[Event]:
        if isinstance(event, str):
            return [e for e in self.events
                    if e.topic == event or e.topic.startswith(event + ".")]
        return [e for e in self.events if isinstance(e, event)]

    @property
    def actions(self) -> Sequence[ActionEvent]:
        return self.events_of(ActionEvent)

    # -- representations (plugin-backed; core table works unbound) ----------
    def render(self, format: str, **options: Any) -> Any:
        if self._client is not None:
            table = self._client._render_table
        else:
            from .plugins.render import default_render_table
            table = default_render_table()
        renderer = table.get((self.kind, format))
        if renderer is None:
            raise LookupError(
                f"no renderer registered for kind={self.kind!r} "
                f"format={format!r}")
        return renderer.render(self, format, **options)

    # -- typed views (aliases of this document, ISSUES #6) ------------------
    def _view(self, cls: type["Document"]) -> Any:
        if isinstance(self, cls):
            return self
        view = self._views.get(cls)
        if view is None:
            values = {name: getattr(self, name) for name in Document.model_fields}
            view = cls.model_construct(**values)
            # Share private state so binding, the parse cache and the view
            # cache alias the original (pinned by tests).
            object.__setattr__(view, "__pydantic_private__",
                               self.__pydantic_private__)
            self._views[cls] = view
        return view

    @property
    def html(self) -> "HTMLDocument":
        return self._view(HTMLDocument)

    @property
    def json(self) -> "JSONDocument":  # type: ignore[override]
        return self._view(JSONDocument)

    @property
    def xml(self) -> "XMLDocument":
        return self._view(XMLDocument)

    @property
    def binary(self) -> "BinaryDocument":
        return self._view(BinaryDocument)

    # -- selection ----------------------------------------------------------
    def _parsed(self) -> Any:
        if self._tree is None:
            from lxml import etree
            from lxml import html as _lxml_html
            if self.kind == "html":
                self._tree = _lxml_html.fromstring(self.content or b"<html></html>")
            elif self.kind == "xml":
                if not self.content:
                    raise ValueError("cannot parse an empty XML document")
                tree = etree.fromstring(
                    self.content, parser=etree.XMLParser(recover=True))
                if tree is None:
                    raise ValueError("XML content is unparseable, even leniently")
                self._tree = tree
            else:
                raise TypeError(
                    "select() requires a parsed tree; "
                    f"this document kind is {self.kind!r}")
        return self._tree

    @overload
    def select(self, selector: str, *, index: int = 0) -> Node: ...
    @overload
    def select(self, selector: str, *, index: int = 0,
               optional: Literal[True]) -> Node | None: ...
    def select(self, selector: str, *, index: int = 0,
               optional: bool = False) -> Node | None:
        root = Node._wrap(self._parsed(), self)
        if optional:
            return root.select(selector, index=index, optional=True)
        return root.select(selector, index=index)

    def select_all(self, selector: str, limit: int | None = None,
                   offset: int = 0) -> Sequence[Node]:
        return Node._wrap(self._parsed(), self).select_all(
            selector, limit=limit, offset=offset)

    # -- pagination ---------------------------------------------------------
    def paginate(self, on: Any, *, until: Any = None,
                 limit: int | None = None, offset: int = 0,
                 resume: "Reference | None" = None, prefetch: int = 1,
                 client: Any = None) -> Any:
        """Iterate pages starting from this one.

        ``on`` builds the next Reference: a CSS/XPath selector whose match's
        href is followed; a callable ``(Document) -> Reference | None``; or
        an iterable of dicts (merged as query params onto this document's
        reference) or References. ``until`` stops iteration, evaluated on
        the next page (selector match or predicate). ``limit`` caps pages
        fetched; ``offset`` skips pages before yielding; ``resume`` restarts
        from the Reference of the last page a prior run yielded.
        ``prefetch`` pages are buffered ahead of the consumer."""
        from .client import default_client
        wc = client or self._client or default_client()
        return wc._paginate(self, on, until, limit, offset, resume, prefetch,
                            self._session)


class HTMLDocument(Document):
    kind: Literal["html", "json", "xml", "binary"] = "html"

    @property
    def title(self) -> str | None:
        node = self.select("title", optional=True)
        return node.text if node is not None else None

    def links(self, selector: str = "a[href]") -> list[Reference]:
        refs = []
        for node in self.select_all(selector):
            ref = node.attr("href", optional=True)
            if ref is not None:
                refs.append(ref)
        return refs

    @property
    def markdown(self) -> str:
        return self.render("markdown")

    @property
    def elements(self) -> list[Element]:
        return self.render("elements")


class JSONDocument(Document):
    kind: Literal["html", "json", "xml", "binary"] = "json"

    @property
    def data(self) -> Any:
        return _json.loads(self.text)

    def query(self, path: str) -> Any:
        """Dotted-path query with [n] indexing, e.g. "items[0].name".
        (JMESPath-lite; a full engine is a later, deliberate dependency.)"""
        import re
        value = self.data
        for token in re.findall(r"[^.\[\]]+|\[\d+\]", path):
            if token.startswith("["):
                value = value[int(token[1:-1])]
            else:
                value = value[token]
        return value


class XMLDocument(Document):
    kind: Literal["html", "json", "xml", "binary"] = "xml"
    # selection accepts css or xpath like any document (elements only);
    # parsing is lenient (recover=True, ISSUES #7)


class BinaryDocument(Document):
    kind: Literal["html", "json", "xml", "binary"] = "binary"
    media_type: str | None = None

    def save(self, path: str) -> str:
        Path(path).write_bytes(self.content)
        return path


# NetworkEvent.request is a forward ref to Reference (events.py must not
# import models at runtime, ISSUES #13); resolve it now.
for _cls in (NetworkEvent, XHREvent, FetchEvent, NavigationEvent, AssetEvent):
    _cls.model_rebuild(_types_namespace={"Reference": Reference})
