"""Live page backing: auto-waiting selection and the interaction set.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Sequence

from ...events import ActionEvent
from ..models import Collection, Document, Field, Reference
from ..base import Capability
from .base import Backing, _timeout_ms, pw_selector

if TYPE_CHECKING:
    from ..document import DocumentCore

#: one round trip per selected element: its html and its DOM identity path
#: (for event narrowing), captured at selection so reads need no loop bridge.
_OUTER_HTML_AND_PATH = (
    "el => ({html: el.outerHTML, "
    "path: window.__wc ? window.__wc.pathOf(el) : null})")


class LiveSelect(Backing):
    provides = frozenset({"select", "select_all", "attr"})
    gate: ClassVar[Capability] = "page"

    async def select(self, core: "DocumentCore", selector: str, *, index: int = 0,
                     wait: float | None = None) -> Document:
        loc = _base(core).locator(pw_selector(selector))
        nth = await _nth(core, loc, selector, index)
        info = await nth.evaluate(_OUTER_HTML_AND_PATH)
        return core.element(content=info["html"].encode(), locator=nth,
                            kind="html", identity=info["path"])

    async def select_all(self, core: "DocumentCore", selector: str,
                         limit: int | None = None,
                         offset: int = 0) -> Collection[Document]:
        loc = _base(core).locator(pw_selector(selector))
        count = await loc.count()
        stop = count if limit is None else min(count, offset + limit)
        out: Collection[Document] = Collection()
        for i in range(offset, stop):
            nth = loc.nth(i)
            info = await nth.evaluate(_OUTER_HTML_AND_PATH)
            out._items.append(core.element(
                content=info["html"].encode(), locator=nth, kind="html",
                identity=info["path"]))
        return out

    async def attr(self, core: "DocumentCore", name: str) -> Field[str] | Reference:
        loc = core.locator or core.page
        if name == "text":
            return Field[str](value=" ".join((await loc.inner_text()).split()))
        if name == "html":
            return Field[str](value=await loc.evaluate("el => el.outerHTML"))
        value = await loc.get_attribute(name)
        if value is None:
            raise LookupError(f"no attribute {name!r} on live element")
        return core.doc.join(value) if name in ("href", "src", "action") else \
            Field[str](value=value)


class LiveAction(Backing):
    provides = frozenset({
        "click", "write", "press", "hover", "scroll", "check",
        "select_option", "upload", "drag", "execute", "evaluate",
        "screenshot", "wait_for", "replay"})
    gate: ClassVar[Capability] = "page"

    async def _act(self, core: "DocumentCore", action: str, *,
                   selector: str | None = None, text: str | None = None,
                   button: str = "left", count: int = 1,
                   delay_ms: int | None = None, clear: bool = True,
                   checked: bool = True, value: Any = None, label: Any = None,
                   index: Any = None, files: Sequence[str] | None = None,
                   target: str | None = None, key: str | None = None,
                   timeout: float | None = None, optional: bool = False) -> Document:
        page = core.page
        ms = _timeout_ms(timeout, core.client.timeout)
        loc = core.locator if selector is None else page.locator(pw_selector(selector))
        _emit(core.doc, action, selector=selector, text=text, key=key, target=target)
        core.doc.actions.append({"op": action, "args": {
            k: v for k, v in (("selector", selector), ("text", text),
                              ("key", key), ("target", target)) if v is not None}})
        try:
            if action == "click":
                await loc.first.click(button=button, click_count=count, timeout=ms)
            elif action == "write":
                if clear:
                    await loc.first.fill(text or "", timeout=ms)
                else:
                    await loc.first.press_sequentially(text or "", delay=delay_ms, timeout=ms)
            elif action == "press":
                await (loc.first.press(key, timeout=ms) if loc is not None
                       else page.keyboard.press(key))
            elif action == "hover":
                await loc.first.hover(timeout=ms)
            elif action == "check":
                await (loc.first.check(timeout=ms) if checked
                       else loc.first.uncheck(timeout=ms))
            elif action == "select_option":
                await loc.first.select_option(value=value, label=label, index=index, timeout=ms)
            elif action == "upload":
                await loc.first.set_input_files(list(files or []), timeout=ms)
            elif action == "drag":
                await page.drag_and_drop(pw_selector(selector or ""),
                                         pw_selector(target or ""), timeout=ms)
            elif action == "scroll":
                target_el = loc.first if loc is not None else None
                if target_el is not None:
                    await target_el.evaluate("(el, d) => el.scrollBy(d.x, d.y)",
                                             {"x": value or 0, "y": index or 0})
                else:
                    await page.evaluate("d => window.scrollBy(d.x, d.y)",
                                        {"x": value or 0, "y": index or 0})
        except LookupError:
            raise
        except Exception as exc:
            if optional and "Timeout" in type(exc).__name__:
                return core.doc
            if "Timeout" in type(exc).__name__:
                raise LookupError(
                    f"{action}: no target for {selector!r} within "
                    f"{ms / 1000:.1f}s") from exc
            raise
        core.invalidate()
        return core.doc

    async def click(self, core: "DocumentCore", selector: str | None = None, *,
                    button: str = "left", count: int = 1,
                    timeout: float | None = None, optional: bool = False) -> Document:
        return await self._act(core, "click", selector=selector, button=button,
                               count=count, timeout=timeout, optional=optional)

    async def write(self, core: "DocumentCore", selector: str, text: str, *,
                    clear: bool = True, delay_ms: int | None = None,
                    timeout: float | None = None, optional: bool = False) -> Document:
        return await self._act(core, "write", selector=selector, text=text,
                               clear=clear, delay_ms=delay_ms, timeout=timeout,
                               optional=optional)

    async def press(self, core: "DocumentCore", key: str, *, selector: str | None = None,
                    timeout: float | None = None) -> Document:
        return await self._act(core, "press", selector=selector, key=key, timeout=timeout)

    async def hover(self, core: "DocumentCore", selector: str, *,
                    timeout: float | None = None, optional: bool = False) -> Document:
        return await self._act(core, "hover", selector=selector, timeout=timeout,
                               optional=optional)

    async def check(self, core: "DocumentCore", selector: str, checked: bool = True, *,
                    timeout: float | None = None) -> Document:
        return await self._act(core, "check", selector=selector, checked=checked,
                               timeout=timeout)

    async def select_option(self, core: "DocumentCore", selector: str, *,
                            value: str | None = None, label: str | None = None,
                            index: int | None = None) -> Document:
        return await self._act(core, "select_option", selector=selector,
                               value=value, label=label, index=index)

    async def upload(self, core: "DocumentCore", selector: str,
                     files: Sequence[str]) -> Document:
        return await self._act(core, "upload", selector=selector, files=files)

    async def drag(self, core: "DocumentCore", source: str, target: str) -> Document:
        return await self._act(core, "drag", selector=source, target=target)

    async def scroll(self, core: "DocumentCore", selector: str | None = None, *,
                     x: int = 0, y: int = 0) -> Document:
        return await self._act(core, "scroll", selector=selector, value=x, index=y)

    async def execute(self, core: "DocumentCore", script: str) -> Document:
        _emit(core.doc, "execute", script=script[:200])
        await core.page.evaluate(script)
        return core.doc

    async def evaluate(self, core: "DocumentCore", script: str) -> Any:
        return await core.page.evaluate(script)

    async def screenshot(self, core: "DocumentCore", selector: str | None = None, *,
                         full_page: bool = False,
                         format: str = "png") -> Document:
        if selector is not None:
            loc = core.page.locator(pw_selector(selector))
            data = await loc.first.screenshot(type=format)
        elif core.locator is not None:
            data = await core.locator.screenshot(type=format)
        else:
            data = await core.page.screenshot(full_page=full_page, type=format)
        return core.binary(data, format)

    async def wait_for(self, core: "DocumentCore", selector: str | None = None, *,
                       event: str | None = None, state: str = "visible",
                       timeout: float | None = None,
                       optional: bool = False) -> Document:
        page = core.page
        if selector is not None:
            ms = _timeout_ms(timeout, core.client.timeout)
            try:
                await page.wait_for_selector(pw_selector(selector), state=state, timeout=ms)
            except Exception as exc:
                if optional and "Timeout" in type(exc).__name__:
                    return core.doc
                raise LookupError(
                    f"wait_for: {selector!r} not {state} within "
                    f"{ms / 1000:.1f}s") from exc
        elif event is not None:
            await page.wait_for_load_state(event)
        elif timeout is not None:
            import asyncio
            await asyncio.sleep(timeout)
        return core.doc

    async def replay(self, core: "DocumentCore",
                     actions: Sequence[dict[str, Any]]) -> Document:
        """Re-apply a recorded action chain (``ref.actions``)."""
        for step in actions:
            if step["op"] == "execute":
                continue                       # scripts are not replayed blindly
            await self._act(core, step["op"], **step.get("args", {}))
        return core.doc



def _base(core: "DocumentCore") -> Any:
    base = core.locator or core.page
    if base is None:
        raise RuntimeError("this document was released or navigated away")
    return base


async def _nth(core: "DocumentCore", locator: Any, selector: str, index: int) -> Any:
    count = await locator.count()
    if count == 0:
        try:
            await locator.first.wait_for(
                state="attached", timeout=_timeout_ms(None, core.client.timeout))
            count = await locator.count()
        except Exception:
            raise LookupError(f"no match for {selector!r}") from None
    position = index if index >= 0 else count + index
    if position < 0 or position >= count:
        raise LookupError(
            f"no match for {selector!r} at index {index} ({count} matches)")
    return locator.nth(position)


def _emit(doc: Any, action: str, **args: Any) -> None:
    doc._client.bus.publish(ActionEvent(
        action=action, args=args, document_id=doc.id,
        session_id=doc.session_id, source="core-action"))


# --------------------------------------------------------------------------- #
# The switch
