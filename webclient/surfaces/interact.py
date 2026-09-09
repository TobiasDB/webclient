"""Interaction. Every op here declares the `browser` capability, so calling
one on an http-backed Document raises `UnsupportedOperation` naming the fix,
and calling one on a Document whose page moved on raises `StaleDocument`."""
from __future__ import annotations

from typing import Any, Literal, Sequence

from ..backing import BROWSER
from ..ops import bind_ops, op
from ..values import Value

_HINT = "Re-resolve with wc.resolve(ref, browser=True)."


@bind_ops
class InteractSurface:
    _backing: Any
    _path: str | None

    @op(capability=BROWSER, resource="page", mutates=True, returns="self",
        hint=_HINT)
    def click(self, selector: str | None = None, *,
              button: Literal["left", "middle", "right"] = "left",
              count: int = 1, timeout: float | None = None,
              optional: bool = False) -> Any:
        return self._act("click", selector, button=button, count=count,
                         timeout=timeout, optional=optional)

    @op(capability=BROWSER, resource="page", mutates=True, returns="self",
        hint=_HINT)
    def write(self, selector: str | None = None, text: str = "", *,
              clear: bool = True, delay_ms: int | None = None,
              timeout: float | None = None, optional: bool = False) -> Any:
        return self._act("write", selector, text=text, clear=clear,
                         delay_ms=delay_ms, timeout=timeout,
                         optional=optional)

    @op(capability=BROWSER, resource="page", mutates=True, returns="self",
        hint=_HINT)
    def press(self, key: str, *, selector: str | None = None,
              timeout: float | None = None) -> Any:
        return self._act("press", selector, key=key, timeout=timeout)

    @op(capability=BROWSER, resource="page", mutates=True, returns="self",
        hint=_HINT)
    def hover(self, selector: str | None = None, *,
              timeout: float | None = None, optional: bool = False) -> Any:
        return self._act("hover", selector, timeout=timeout,
                         optional=optional)

    @op(capability=BROWSER, resource="page", mutates=True, returns="self",
        hint=_HINT)
    def check(self, selector: str | None = None, checked: bool = True, *,
              timeout: float | None = None) -> Any:
        return self._act("check", selector, checked=checked, timeout=timeout)

    @op(capability=BROWSER, resource="page", mutates=True, returns="self",
        hint=_HINT)
    def select_option(self, selector: str | None = None, *,
                      value: str | None = None, label: str | None = None,
                      index: int | None = None) -> Any:
        return self._act("select_option", selector, value=value, label=label,
                         index=index)

    @op(capability=BROWSER, resource="page", mutates=True, returns="self",
        hint=_HINT)
    def upload(self, selector: str, files: Sequence[str]) -> Any:
        return self._act("upload", selector, files=list(files))

    @op(capability=BROWSER, resource="page", mutates=True, returns="self",
        hint=_HINT)
    def scroll(self, selector: str | None = None, *, x: int = 0,
               y: int = 0) -> Any:
        return self._act("scroll", selector, x=x, y=y)

    def _act(self, action: str, selector: str | None, **args: Any) -> Any:
        from .._async import chain
        return chain(
            self._backing.act(action, self._path, selector, **args),
            lambda _: self)
