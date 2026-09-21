"""JsonBacking: dotted-path ops for json documents."""

from __future__ import annotations

import json as _json
import re
from typing import TYPE_CHECKING, Any

from ...dom import parse_json
from ...dom.json import json_skeleton
from ...query.collection import Field
from ..web_core import Backing
from .models import Element

if TYPE_CHECKING:
    from . import Document


def _strip(v: Any) -> Any:
    """Trim surrounding whitespace from a string leaf (an accessor reads a value, not its
    padding); non-strings pass through unchanged."""
    return v.strip() if isinstance(v, str) else v


#: back-compat alias -- the outline now lives in :mod:`webclient.dom.json`.
_json_skeleton = json_skeleton


def _json_elements(value: Any) -> list[Element]:
    """Flatten a parsed JSON value into a list of text ``Element``s, one per scalar leaf,
    each keyed by its dotted/indexed path (so JSON exposes the same element surface as HTML)."""
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


class JsonBacking(Backing):
    """Dotted-path ops for json. A selected node is a Document holding the
    sub-value; ``attr('value')`` (typed) / ``attr('text')`` (as a string) read it."""

    provides = frozenset({"select", "select_all", "attr", "render", "elements", "skeleton"})
    collections = frozenset({"select_all"})
    gate = "tree"

    def elements(self, core: "Document") -> "list[Element]":
        """The json as a flat list of typed content blocks (dotted-path ids)."""
        return _json_elements(self._data(core))

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
    ) -> str:
        """A token-lean JSON shape outline (keys + value types, arrays as ``[N]`` with
        their element shape) so an LLM can write dotted-path queries against an API /
        JSON document -- the JSON twin of the DOM skeleton. The extra DOM-only kwargs
        are accepted for a uniform ``skeleton`` signature and ignored here."""
        return _json_skeleton(
            self._data(core),
            max_lines=max_lines,
            text_chars=text_chars,
            max_depth=max_depth,
        )

    def applies(self, core: "Document") -> bool:
        """In play only for json documents."""
        return core.kind == "json"

    def select_all(
        self,
        core: "Document",
        path: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> "list[Document]":
        """Each item of the array at ``path`` as its own sub-document (``offset``/``limit``
        slice the list). A missing path or non-array node is an empty collection, not an error."""
        # optional: a missing path (or a non-array node) is an EMPTY collection, not an error --
        # matching HtmlBacking.select_all's "an empty match is still a collection" contract, so a
        # fan_out over the result doesn't raise and cancel its siblings.
        node = self.select(core, path, optional=True)
        data = None if node._missing else node._element
        items = list(data) if isinstance(data, list) else []
        items = items[offset:]
        if limit is not None:
            items = items[:limit]
        return [core._sub(item) for item in items]

    def render(self, core: "Document", format: str, **options: Any) -> "list[Element]":
        """Render the json to the ``"elements"`` format (its flat leaf list); any other
        format is unsupported and raises."""
        if format != "elements":
            from ...errors import render_error

            raise render_error(f"no json render format {format!r}")
        return _json_elements(self._data(core))

    def _data(self, core: "Document") -> Any:
        """The document's parsed JSON value -- a selected sub-value if this is a selected node,
        else the body parsed once and cached (``None`` on a body that won't parse)."""
        if core._element is not None:
            return core._element  # a selected sub-value
        if core._data is None:
            # a mislabelled / truncated body must not crash a lenient caller -> "no data"
            core._data = parse_json(core.content)
        return core._data

    def select(
        self,
        core: "Document",
        path: str,
        *,
        index: int = 0,  # unified with html select; json paths address nodes directly
        optional: bool = False,
        error: Any = None,
    ) -> "Document":
        """Navigate a dotted/indexed ``path`` (``a.b[0].c``) to a sub-value, returned as a
        sub-document. A missing path is a miss -- an empty document when ``optional``, else the
        configured error."""
        from ...errors import RETURN

        from .html import _miss

        value = self._data(core)
        missed = False
        try:
            for tok in re.findall(r"[^.\[\]]+|\[\d+\]", path):
                value = value[int(tok[1:-1])] if tok.startswith("[") else value[tok]
        except (KeyError, IndexError, TypeError):
            missed = True
        if missed:  # a genuine miss: raise/return like html (consistent contract)
            return _miss(core, f"no match for {path!r}", RETURN if optional else error)
        return core._sub(value)

    def attr(
        self, core: "Document", name: str, pattern: str | None = None, *,
        group: int | str | None = None, optional: bool = False, error: Any = None,
    ) -> "Field[Any]":
        """A value off this JSON node: ``"value"`` is the node's own value (kept typed --
        a number stays a number); ``"text"`` is its value as a string (a scalar as text,
        an object/array as JSON); any other ``name`` is a key of an object node. ``pattern``
        extracts a substring by regex (see :meth:`HtmlBacking.attr`); a non-match is a miss."""
        from .html import _regex_field

        if core._missing:
            return Field(None, ok=False)
        data = self._data(core)
        if name == "value":  # the node's own value, kept typed
            return _regex_field(_strip(data), pattern, group)
        if name == "text":  # the node's value as a string
            text = data.strip() if isinstance(data, str) else _json.dumps(data)
            return _regex_field(text, pattern, group)
        if isinstance(data, dict) and name in data:
            return _regex_field(_strip(data[name]), pattern, group)
        # a missing key: same contract as html attr -- raise (structured) by default,
        # a not-ok Field under optional / RETURN. (Never silently return the node.)
        from ...errors import RAISE, current_policy, select_error

        if not optional and (error or current_policy()) is RAISE:
            raise select_error(f"no key {name!r}")
        return Field(None, ok=False)


__all__ = ["JsonBacking"]
