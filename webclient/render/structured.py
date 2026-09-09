"""Representations for json and binary documents."""
from __future__ import annotations

import json as _json
from typing import Any

from . import Block, RendererRegistry


def json_elements(document: Any) -> list[Block]:
    """Flatten a parsed body into `path -> value` leaf blocks."""
    data = _json.loads(document._backing.text)
    out: list[Block] = []

    def walk(value: Any, path: str, parent: str | None) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{path}.{key}" if path else key, path or None)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]", path or None)
        else:
            out.append(Block(id=path, type="text", text=str(value),
                             parent_id=parent))

    walk(data, "", None)
    return out


def json_text(document: Any, *, indent: int = 2) -> str:
    return _json.dumps(_json.loads(document._backing.text), indent=indent)


def install(registry: RendererRegistry) -> None:
    registry.register("json", "elements", json_elements)
    registry.register("json", "markdown", json_text)
    registry.register("json", "readable", json_text)
    registry.register("json", "html", lambda d: d._backing.text)
