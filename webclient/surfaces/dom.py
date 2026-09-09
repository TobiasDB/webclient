"""Waiting on and scripting a live DOM."""
from __future__ import annotations

from typing import Any, Literal

from ..backing import BROWSER
from ..ops import bind_ops, op
from ..values import Value

_HINT = "Re-resolve with wc.resolve(ref, browser=True)."


@bind_ops
class DomSurface:
    _backing: Any
    _path: str | None

    @op(capability=BROWSER, resource="page", returns="self", hint=_HINT)
    def wait_stable(self, *, quiet_ms: int = 500,
                    timeout: float | None = None) -> Any:
        """Block until the DOM stops mutating for `quiet_ms`. A quiet period,
        not a fixed sleep."""
        from .._async import chain
        return chain(self._backing.wait_stable(quiet_ms=quiet_ms,
                                               timeout=timeout),
                     lambda _: self)

    @op(capability=BROWSER, resource="page", returns="self", hint=_HINT)
    def wait_for(self, selector: str | None = None, *,
                 state: Literal["attached", "visible", "hidden",
                                "detached"] = "visible",
                 event: str | None = None,
                 timeout: float | None = None,
                 optional: bool = False) -> Any:
        from .._async import chain
        return chain(self._backing.wait_for(selector, state=state,
                                            event=event, timeout=timeout,
                                            optional=optional),
                     lambda _: self)

    @op(capability=BROWSER, resource="page", returns="Value", hint=_HINT)
    def evaluate(self, script: str) -> Value[Any]:
        """Run JS in the page and return its result."""
        from .._async import chain
        return chain(self._backing.evaluate(script, self._path), Value)

    @op(capability=BROWSER, resource="page", returns="Document", hint=_HINT)
    def screenshot(self, selector: str | None = None, *,
                   full_page: bool = False,
                   format: Literal["png", "jpeg"] = "png") -> Any:
        """A binary Document holding the image."""
        from .._async import chain
        from ..backing import StaticBacking
        from ..document import Document
        target = selector

        def wrap(data: bytes) -> Any:
            backing = StaticBacking(data, "binary", status_code=200,
                                    response_headers={
                                        "content-type": f"image/{format}"})
            return Document(backing=backing,
                            request=getattr(self, "request", None))

        return chain(self._backing.screenshot(self._path, target,
                                              full_page=full_page,
                                              format=format), wrap)
