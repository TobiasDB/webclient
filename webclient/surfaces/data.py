"""Structured-data access for json documents."""
from __future__ import annotations

import json as _json
import re
from typing import Any

from ..errors import SelectionError
from ..ops import bind_ops, op
from ..values import Value


@bind_ops
class DataSurface:
    _backing: Any

    @op(pure=True, returns="Value", cardinality="one->one")
    def data(self) -> Value[Any]:
        """The parsed body of a json document."""
        try:
            return Value(_json.loads(self._backing.text))
        except ValueError as exc:
            raise SelectionError(f"document is not valid JSON: {exc}") from None

    @op(pure=True, returns="Value", cardinality="one->one")
    def query(self, path: str, *, optional: bool = False) -> Value[Any]:
        """Dotted path with `[n]` indexing, e.g. `items[0].name`."""
        value: Any = _json.loads(self._backing.text)
        for token in re.findall(r"[^.\[\]]+|\[\d+\]", path):
            try:
                if token.startswith("["):
                    value = value[int(token[1:-1])]
                else:
                    value = value[token]
            except (KeyError, IndexError, TypeError):
                if optional:
                    return Value(None)
                raise SelectionError(
                    f"no value at {path!r} (stopped at {token!r})") from None
        return Value(value)
