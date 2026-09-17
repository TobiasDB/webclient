"""JsonBacking: dotted-path ops for json documents."""

from __future__ import annotations

import json as _json
import re
from typing import TYPE_CHECKING, Any

from ...collection import Field
from ..web_core import Backing
from .models import Element

if TYPE_CHECKING:
    from . import Document


def _strip(v: Any) -> Any:
    """Trim surrounding whitespace from a string leaf (an accessor reads a value, not its
    padding); non-strings pass through unchanged."""
    return v.strip() if isinstance(v, str) else v


def _json_type(v: Any) -> str:
    """The shape name of a JSON scalar (objects/arrays are rendered structurally)."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    return "string"


def _merge_keys(items: list[Any]) -> "dict[str, Any]":
    """A representative object for an array of objects: the union of keys, each mapped
    to a sample value (so ``results[].name`` paths are visible from one element)."""
    merged: dict[str, Any] = {}
    for it in items:
        if isinstance(it, dict):
            for k, v in it.items():
                if k not in merged or merged[k] in (None, "", [], {}):
                    merged[k] = v
    return merged


def _json_skeleton(
    data: Any, *, max_lines: int = 400, text_chars: int = 40, max_depth: int = 30
) -> str:
    """A token-lean JSON shape outline: keys with value types, an array as ``[N]`` with
    its element shape (object keys merged across items), nested paths kept -- so an LLM
    can write dotted-path queries (``select('results[0].name')`` / ``extract``). The
    JSON twin of the DOM skeleton; a sample scalar is shown, truncated to
    ``text_chars``."""
    lines: list[str] = []

    def sample(v: Any) -> str:
        s = str(v)
        return s if len(s) <= text_chars else s[:text_chars] + "…"

    def walk(v: Any, key: str, depth: int) -> None:
        if len(lines) >= max_lines or depth > max_depth:
            return
        pad = "  " * depth
        label = f"{key}: " if key else ""
        if isinstance(v, dict):
            lines.append(f"{pad}{label}{{}}" if not v else f"{pad}{label}{{")
            if v:
                for k, item in v.items():
                    walk(item, k, depth + 1)
                lines.append(f"{pad}}}")
        elif isinstance(v, list):
            lines.append(f"{pad}{label}[{len(v)}]")
            if v:  # show one representative element's shape (merged object keys)
                rep = _merge_keys(v) if any(isinstance(i, dict) for i in v) else v[0]
                walk(rep, "", depth + 1)
        else:
            lines.append(f"{pad}{label}{_json_type(v)}  = {sample(v)}")

    walk(data, "", 0)
    return "\n".join(lines[:max_lines])


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


class JsonBacking(Backing):
    """Dotted-path ops for json. A selected node is a Document holding the
    sub-value; ``attr('value')`` / ``text_content`` read it."""

    provides = frozenset({"select", "select_all", "attr", "render", "elements", "skeleton"})
    collections = frozenset({"select_all"})
    props = frozenset({"text_content"})
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
        return core.kind == "json"

    def select_all(
        self,
        core: "Document",
        path: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> "list[Document]":
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
        if format != "elements":
            from ...errors import render_error

            raise render_error(f"no json render format {format!r}")
        return _json_elements(self._data(core))

    def _data(self, core: "Document") -> Any:
        if core._element is not None:
            return core._element  # a selected sub-value
        if core._data is None:
            try:  # a mislabelled / truncated body must not crash a lenient caller
                core._data = _json.loads(core.content or b"null")
            except ValueError:
                core._data = None  # degrade to "no data" (like html's empty tree)
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
        self, core: "Document", name: str, *, optional: bool = False, error: Any = None
    ) -> "Field[Any]":
        from ...errors import RAISE, current_policy, select_error

        if core._missing:
            return Field(None, ok=False)
        data = self._data(core)
        if name == "value":  # the node's own value
            return Field(_strip(data))
        if isinstance(data, dict) and name in data:
            return Field(_strip(data[name]))
        # a missing key: same contract as html attr -- raise (structured) by default,
        # a not-ok Field under optional / RETURN. (Never silently return the node.)
        if not optional and (error or current_policy()) is RAISE:
            raise select_error(f"no key {name!r}")
        return Field(None, ok=False)

    def text_content(self, core: "Document") -> "str | None":
        if core._missing:
            return None
        value = self._data(core)
        return value.strip() if isinstance(value, str) else _json.dumps(value)


__all__ = ["JsonBacking"]
