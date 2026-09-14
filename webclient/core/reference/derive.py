"""DeriveBacking: pure request-spec derivations (no IO). ``url`` is a property
op; ``with_params``/``replace``/``join`` return a fresh ``ReferenceCore``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urljoin

from ..web_core import Backing

if TYPE_CHECKING:
    from . import ReferenceCore

DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}


class DeriveBacking(Backing):
    """Pure request-spec derivations (no IO). ``url`` is a property op; the
    rest return a fresh ``ReferenceCore``."""

    props = frozenset({"url"})
    provides = frozenset({"with_params", "replace", "join"})
    gate = "ok"

    def url(self, core: "ReferenceCore") -> str:
        port = ""
        if core.port is not None and core.port != DEFAULT_PORTS.get(core.scheme):
            port = f":{core.port}"
        out = f"{core.scheme}://{core.hostname}{port}{core.path}"
        if core.params:
            out += "?" + urlencode(core.params, doseq=True)
        if core.fragment:
            out += "#" + core.fragment
        return out

    def replace(self, core: "ReferenceCore", **fields: Any) -> "ReferenceCore":
        return core._derive(core.model_copy(update=fields))

    def with_params(self, core: "ReferenceCore", **params: str) -> "ReferenceCore":
        return core._derive(
            core.model_copy(update={"params": {**core.params, **params}})
        )

    def join(self, core: "ReferenceCore", href: str) -> "ReferenceCore":
        from . import from_url

        return from_url(urljoin(self.url(core), href))
