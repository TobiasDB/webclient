"""DocumentCore: the core behind a document (MVP).

Core Fields = the resolved response (the surface's data). Backings = per-medium
op providers: HtmlBacking (css/xpath select, attr, text) and JsonBacking (dotted
path). A selected element is itself a DocumentCore (subtree / json sub-value),
so selection nests. Render / live / events are later slices.
"""

from __future__ import annotations

import copy
import json as _json
import re
from typing import TYPE_CHECKING, Any, ClassVar, Literal
from urllib.parse import urljoin

from pydantic import BaseModel, PrivateAttr

from ..errors import WebError
from .live import LiveBacking
from .reference_core import ReferenceCore, from_url
from .web_core import Backing, WebCore

if TYPE_CHECKING:
    from .client_core import WebClientCore

_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_SKIP = {"script", "style"}
_NOISE = "script, style, nav, aside, footer, header"
_MAIN = "main, article, [role=main], #content, #main"


class Element(BaseModel):
    """A typed content block -- the "elements" representation."""

    id: str = ""
    type: str = "text"
    text: str = ""
    parent_id: str | None = None
    metadata: dict[str, Any] = {}


def _norm(text: str) -> str:
    return " ".join(text.split())


def _tag(el: Any) -> str:
    return el.tag.lower() if isinstance(el.tag, str) else ""


def _inline(el: Any) -> str:
    parts = [el.text or ""]
    for child in el:
        tag = _tag(child)
        if tag in _SKIP:
            parts.append(child.tail or "")
            continue
        inner = _inline(child)
        if tag == "a":
            parts.append(f"[{inner}]({child.get('href', '')})")
        elif tag in ("strong", "b"):
            parts.append(f"**{inner}**")
        elif tag in ("em", "i"):
            parts.append(f"*{inner}*")
        elif tag == "code":
            parts.append(f"`{inner}`")
        elif tag == "img":
            parts.append(f"![{child.get('alt', '')}]({child.get('src', '')})")
        else:
            parts.append(inner)
        parts.append(child.tail or "")
    return _norm("".join(parts))


def _md_blocks(el: Any, out: list[str]) -> None:
    for child in el:
        tag = _tag(child)
        if tag in _SKIP:
            continue
        if tag in _HEADINGS:
            out.append("#" * int(tag[1]) + " " + _inline(child))
        elif tag == "p":
            out.append(_inline(child))
        elif tag in ("ul", "ol"):
            items = [
                f"{'-' if tag == 'ul' else str(i + 1) + '.'} {_inline(li)}"
                for i, li in enumerate(child.findall("li"))
            ]
            if items:
                out.append("\n".join(items))
        elif tag == "pre":
            out.append("```\n" + "".join(child.itertext()).strip("\n") + "\n```")
        elif tag == "blockquote":
            out.append("> " + _inline(child))
        elif tag == "img":
            out.append(f"![{child.get('alt', '')}]({child.get('src', '')})")
        else:
            _md_blocks(child, out)


def _main_container(root: Any) -> Any:
    found = root.cssselect(_MAIN)
    return found[0] if found else root


def _html_elements(root: Any) -> list[Element]:
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


def _json_elements(value: Any) -> list[Element]:
    out: list[Element] = []

    def walk(v: Any, path: str, parent: str | None) -> None:
        if isinstance(v, dict):
            for k, item in v.items():
                walk(item, f"{path}.{k}" if path else k, path or None)
        elif isinstance(v, list):
            for i, item in enumerate(v):
                walk(item, f"{path}[{i}]", path or None)
        else:
            out.append(Element(id=path, type="text", text=str(v), parent_id=parent))

    walk(value, "", None)
    return out


class StatusBacking(Backing):
    """Status / value ops, available even on a not-ok document: ``is_ok`` /
    ``is_empty`` (a ``Field``), ``message`` (the error text)."""

    provides = frozenset({"is_ok", "is_empty", "ref", "summary", "reload"})
    props = frozenset({"message"})
    gate = "ok"

    def applies(self, core: "DocumentCore") -> bool:
        return True

    def ref(self, core: "DocumentCore") -> "ReferenceCore | None":
        """The reference that produced this document (for reload / recovery)."""
        return core._ref

    def reload(self, core: "DocumentCore") -> "DocumentCore":
        """Re-resolve on a fresh page, replaying the recorded action chain --
        available even after the page was released."""
        return core._client.loop().run(core._client._areload(core))

    def summary(self, core: "DocumentCore") -> dict[str, Any]:
        """A page digest: url / ok, plus title + markdown when available."""
        out: dict[str, Any] = {"url": core.final_url or core.url, "ok": core.ok}
        if core.has_op("title"):
            out["title"] = core.dispatch("title")
        if core.has_op("render"):
            out["markdown"] = core.dispatch("render", "markdown")
        return out

    def is_ok(self, core: "DocumentCore") -> Any:
        from ..collection import Field

        return Field(core.ok)

    def is_empty(self, core: "DocumentCore") -> Any:
        from ..collection import Field

        empty = core._missing or not core.ok or not (core.content or core._element)
        return Field(bool(empty))

    def message(self, core: "DocumentCore") -> str:
        return core.error.message if core.error is not None else ""


class EventBacking(Backing):
    """Events routed onto the document. ``events`` is everything captured;
    ``events_of(cls)`` narrows by type; ``action_events`` is the interaction
    subset (empty until a live/browser document)."""

    provides = frozenset({"events_of"})
    props = frozenset({"events", "action_events"})
    gate = "ok"

    def applies(self, core: "DocumentCore") -> bool:
        return True

    def events(self, core: "DocumentCore") -> list[Any]:
        return core._events  # the live store (appendable)

    def events_of(self, core: "DocumentCore", event_type: Any) -> list[Any]:
        if isinstance(event_type, str):  # a topic prefix
            from ..events import _topic_matches

            return [e for e in core._events if _topic_matches(event_type, e.topic)]
        return [e for e in core._events if isinstance(e, event_type)]

    def action_events(self, core: "DocumentCore") -> list[Any]:
        from ..events import ActionEvent

        return [e for e in core._events if isinstance(e, ActionEvent)]


class HtmlBacking(Backing):
    """Tree ops for html/xml. ``select``/``select_all`` yield element
    DocumentCores; ``attr``/``text`` read from the element (or body)."""

    provides = frozenset({"select", "select_all", "attr", "render"})
    props = frozenset({"text", "title"})
    gate = "tree"

    def applies(self, core: "DocumentCore") -> bool:
        return core.kind in ("html", "xml")

    def title(self, core: "DocumentCore") -> str | None:
        node = self._find(core, "title")
        return _norm("".join(node[0].itertext())) if node else None

    def render(self, core: "DocumentCore", format: str, **options: Any) -> Any:
        override = _override(core, format)
        if override is not None:
            return override
        if format == "html":
            return (core.content or b"").decode(core.encoding or "utf-8", "replace")
        root = self._tree(core)
        if format == "links":
            return [
                from_url(urljoin(core.final_url or core.url, el.get("href")))
                for el in root.cssselect("a[href]")
                if el.get("href")
            ]
        if format == "markdown":
            target = _main_container(root) if options.get("main_content_only") else root
            blocks: list[str] = []
            _md_blocks(target, blocks)
            return "\n\n".join(b for b in blocks if b.strip())
        if format == "text":
            target = copy.deepcopy(_main_container(root))
            for noise in target.cssselect(_NOISE):
                if noise.getparent() is not None:
                    noise.getparent().remove(noise)
            blocks = [
                _norm("".join(el.itertext()))
                for el in target.iter()
                if _tag(el) in _HEADINGS or _tag(el) in ("p", "li", "pre", "blockquote")
            ]
            return "\n\n".join(b for b in blocks if b) or _norm(
                "".join(target.itertext())
            )
        if format == "elements":
            return _html_elements(root)
        raise LookupError(f"no html render format {format!r}")

    def _tree(self, core: "DocumentCore") -> Any:
        if core._element is not None:
            return core._element
        if core._tree is None:
            from lxml import html as _lh

            core._tree = _lh.fromstring(_decode(core) or "<html></html>")
        return core._tree

    def _find(self, core: "DocumentCore", selector: str) -> list[Any]:
        if selector.rstrip().endswith(("text()",)) or "/@" in selector:
            raise ValueError(
                "select yields elements; use .attr() for an attribute or text"
            )
        root = self._tree(core)
        if selector.startswith("/") or selector.startswith("./"):
            return list(root.xpath(selector))
        return list(root.cssselect(selector))

    def select(
        self, core: "DocumentCore", selector: str, *, index: int = 0, error: Any = None
    ) -> "DocumentCore":
        els = self._find(core, selector)
        if not (-len(els) <= index < len(els)):
            return _miss(core, f"no match for {selector!r}", error)
        return _element(core, els[index])

    def select_all(
        self,
        core: "DocumentCore",
        selector: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> "list[DocumentCore]":
        els = self._find(core, selector)[offset:]
        if limit is not None:
            els = els[:limit]
        return [_element(core, el) for el in els]

    def attr(self, core: "DocumentCore", name: str, *, error: Any = None) -> Any:
        from ..collection import Field

        if core._missing:
            return Field(None, ok=False)
        if name == "text":
            return Field(self.text(core))
        el = core._element
        value = el.get(name) if el is not None else None
        if name in ("href", "src", "action"):
            ref = from_url(urljoin(core.final_url or core.url, value or ""))
            ref._client = core._client  # inherit the client so it resolves
            return ref
        if value is None:  # absent attribute
            from ..errors import RAISE, current_policy

            if (error or current_policy()) is RAISE:
                raise LookupError(f"no attribute {name!r}")
            return Field(None, ok=False)
        return Field(value)

    def text(self, core: "DocumentCore") -> "str | None":
        if core._missing:
            return None
        el = core._element if core._element is not None else self._tree(core)
        return _norm("".join(el.itertext()))


class JsonBacking(Backing):
    """Dotted-path ops for json. A selected node is a DocumentCore holding the
    sub-value; ``attr('value')`` / ``text`` read it."""

    provides = frozenset({"select", "select_all", "attr", "render"})
    props = frozenset({"text"})
    gate = "tree"

    def applies(self, core: "DocumentCore") -> bool:
        return core.kind == "json"

    def select_all(
        self,
        core: "DocumentCore",
        path: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> "list[DocumentCore]":
        node = self.select(core, path)
        data = None if node._missing else node._element
        items = list(data) if isinstance(data, list) else []
        items = items[offset:]
        if limit is not None:
            items = items[:limit]
        return [_element(core, item) for item in items]

    def render(self, core: "DocumentCore", format: str, **options: Any) -> Any:
        override = _override(core, format)
        if override is not None:
            return override
        if format != "elements":
            raise LookupError(f"no json render format {format!r}")
        return _json_elements(self._data(core))

    def _data(self, core: "DocumentCore") -> Any:
        if core._element is not None:
            return core._element  # a selected sub-value
        if core._data is None:
            core._data = _json.loads(core.content or b"null")
        return core._data

    def select(self, core: "DocumentCore", path: str) -> "DocumentCore":
        import re

        value = self._data(core)
        try:
            for tok in re.findall(r"[^.\[\]]+|\[\d+\]", path):
                value = value[int(tok[1:-1])] if tok.startswith("[") else value[tok]
        except (KeyError, IndexError, TypeError):
            value = None
        return _element(core, value)

    def attr(self, core: "DocumentCore", name: str, *, error: Any = None) -> Any:
        from ..collection import Field

        if core._missing:
            return Field(None, ok=False)
        data = self._data(core)
        if name != "value" and isinstance(data, dict) and name in data:
            return Field(data[name])
        return Field(data)

    def text(self, core: "DocumentCore") -> "str | None":
        if core._missing:
            return None
        value = self._data(core)
        return value if isinstance(value, str) else _json.dumps(value)


def _override(core: "DocumentCore", format: str) -> Any:
    """A registered ``Renderer`` override for (kind, format), applied to the
    document surface -- else ``None`` (use the built-in render)."""
    client = core._client
    table = getattr(client, "_render_table", None) if client is not None else None
    renderer = table.get((core.kind, format)) if table else None
    if renderer is None:
        return None
    from ..surface import wrap

    return renderer.render(wrap(core), format)


def _decode(core: "DocumentCore") -> str:
    """Decode the response bytes: the declared encoding first, else utf-8 with
    a latin-1 fallback (covers undeclared single-byte pages)."""
    if isinstance(core._element, str):
        return core._element
    if core.encoding:
        return (core.content or b"").decode(core.encoding, "replace")
    try:
        return (core.content or b"").decode("utf-8")
    except UnicodeDecodeError:
        return (core.content or b"").decode("latin-1", "replace")


def _miss(parent: "DocumentCore", message: str, error: Any) -> "DocumentCore":
    """A missing selection: raise under RAISE, else a not-ok sub-document."""
    from ..errors import RAISE, WebError, current_policy

    if (error or current_policy()) is RAISE:
        raise LookupError(message)
    sub = _element(parent, None)
    sub.error = WebError(type="LookupError", message=message)
    return sub


def _element(parent: "DocumentCore", node: Any) -> "DocumentCore":
    """A selected element/value as a DocumentCore rooted at ``parent``. A
    ``None`` node means the selection missed -- a not-ok, empty sub-document."""
    content = b""
    if node is not None and not isinstance(node, (str, int, float, bool, list, dict)):
        try:
            from lxml import html as _lh

            content = _lh.tostring(node)  # the element's own bytes
        except Exception:
            content = b""
    sub = DocumentCore(
        url=parent.url,
        final_url=parent.final_url,
        kind=parent.kind,
        status_code=parent.status_code,
        content=content,
    )
    sub.root = parent.name or parent.root
    sub._client = parent._client
    sub._element = node
    sub._missing = node is None
    sub._events = parent._events  # a static element shares the store
    return sub


class DocumentCore(WebCore, BaseModel):
    """A resolved resource's core (+ element sub-cores). Core Fields are the
    response; behaviour is the backings."""

    id: str = ""
    name: str = ""  # scoped document name
    root: str = ""  # the originating reference's name
    session_id: str = ""  # owning session (if any)
    kind: Literal["html", "json", "xml", "binary"] = "html"
    url: str = ""
    final_url: str | None = None
    content: bytes = b""
    status_code: int = 0
    response_headers: dict[str, str] = {}
    encoding: str | None = None
    elapsed: float | None = None
    created: float = 0.0
    accessed: float = 0.0
    error: WebError | None = None

    _client: Any = PrivateAttr(default=None)  # owning WebClientCore
    _ref: Any = PrivateAttr(default=None)  # the ReferenceCore that produced it
    _element: Any = PrivateAttr(default=None)  # lxml element / json sub-value
    _tree: Any = PrivateAttr(default=None)  # cached lxml parse
    _data: Any = PrivateAttr(default=None)  # cached json
    _missing: bool = PrivateAttr(default=False)  # a selection that missed
    _events: list = PrivateAttr(default_factory=list)  # events routed here
    _page: Any = PrivateAttr(default=None)  # playwright Page (live document)
    _row: Any = PrivateAttr(default=None)  # extracted columns (extract/field)
    _surface: Any = PrivateAttr(default=None)  # the core's single eager surface

    BACKINGS: ClassVar[tuple[Backing, ...]] = (
        StatusBacking(),
        EventBacking(),
        LiveBacking(),
        HtmlBacking(),
        JsonBacking(),
    )

    @property
    def ok(self) -> bool:
        if self.error is not None or self._missing:
            return False
        return 200 <= self.status_code < 300 or self.status_code == 0


__all__ = ["DocumentCore", "HtmlBacking", "JsonBacking"]
