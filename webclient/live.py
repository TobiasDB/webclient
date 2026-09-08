"""LiveDocument / LiveNode: browser-backed, stateful documents (M4).

Every interaction auto-waits (playwright) and raises LookupError on a
missing target unless ``optional=True`` -- same error philosophy as the
static side. Interactions are recorded and emitted as ActionEvents;
``replay`` re-applies a recording. Lifecycle is owned by the WebClient:
``wc.release(live)``, session close or client close return the page.
"""
from __future__ import annotations

from typing import Any, Literal, Sequence

from pydantic import PrivateAttr
from typing_extensions import Self

from .events import ActionEvent, ConsoleEvent, DOMUpdateEvent, Event, Topic, XHREvent
from .models import BinaryDocument, Document, FetchError, Node, Reference, _is_xpath


def _pw_selector(selector: str) -> str:
    return f"xpath={selector}" if _is_xpath(selector) else selector


def _timeout_ms(timeout: float | None, default: float) -> float:
    return (timeout if timeout is not None else default) * 1000


class LiveNode(Node):
    """An element inside a LiveDocument, backed by a playwright locator."""

    _locator: Any = PrivateAttr(default=None)
    _live: Any = PrivateAttr(default=None)     # owning LiveDocument

    @classmethod
    def _wrap_locator(cls, locator: Any, live: "LiveDocument") -> "LiveNode":
        node = cls()
        node._locator = locator
        node._live = live
        node._document = live
        return node

    def _run(self, coro: Any) -> Any:
        return self._live._run(coro)

    @property
    def text(self) -> str:
        return " ".join(self._run(self._locator.inner_text()).split())

    @property
    def html(self) -> str:
        return self._run(self._locator.evaluate("el => el.outerHTML"))

    def attr(self, name: str, *, optional: bool = False) -> Any:
        value = self._run(self._locator.get_attribute(name))
        if value is None:
            if optional:
                return None
            raise LookupError(f"no attribute {name!r} on live element")
        if name in ("href", "src", "action"):
            return self._live.join(value)
        return value

    def select(self, selector: str, *, index: int = 0,
               optional: bool = False) -> Any:
        locator = self._locator.locator(_pw_selector(selector))
        return self._live._nth(locator, selector, index, optional)

    def select_all(self, selector: str, limit: int | None = None,
                   offset: int = 0) -> Sequence["LiveNode"]:
        locator = self._locator.locator(_pw_selector(selector))
        return self._live._all(locator, limit, offset)

    # -- interactions --------------------------------------------------------
    def click(self, **kwargs: Any) -> Self:
        self._live._act("click", locator=self._locator, **kwargs)
        return self

    def write(self, text: str, **kwargs: Any) -> Self:
        self._live._act("write", locator=self._locator, text=text, **kwargs)
        return self

    def hover(self, **kwargs: Any) -> Self:
        self._live._act("hover", locator=self._locator, **kwargs)
        return self

    def scroll_into_view(self) -> Self:
        self._run(self._locator.scroll_into_view_if_needed())
        return self

    def screenshot(self) -> BinaryDocument:
        return self._live._screenshot_of(self._locator)

    # -- element-scoped events (node-identity narrowing, ISSUES #9) ----------
    @property
    def identity_path(self) -> str | None:
        """This element's capture identity ("n1/n4/n9"), if dom capture is
        active on the page."""
        try:
            return self._run(self._locator.evaluate(
                "el => window.__wc ? window.__wc.pathOf(el) : null"))
        except Exception:
            return None

    def events_of(self, event: Any) -> Sequence[Event]:  # type: ignore[override]
        matches = self._live.events_of(event)
        path = self.identity_path
        if path is None:
            return matches
        return [e for e in matches
                if e.node_id is not None
                and (e.node_id == path or e.node_id.startswith(path + "/"))]


class LiveDocument(Document):
    """A playwright-backed page. ``select`` observes the live DOM;
    ``content``/``text`` are the snapshot taken at load. ``navigate``
    returns a NEW LiveDocument reusing the page (the old one refuses
    further actions)."""

    _page: Any = PrivateAttr(default=None)
    _lease: Any = PrivateAttr(default=None)
    _routing: Any = PrivateAttr(default=None)      # bus subscription
    _attached: list[Any] = PrivateAttr(default_factory=list)

    def _run(self, coro: Any) -> Any:
        if self._page is None:
            raise RuntimeError(
                "this LiveDocument was released or navigated away")
        return self._client._ensure_loop().run(coro)

    def _emit_action(self, action: str, **args: Any) -> None:
        event = ActionEvent(action=action, args=args,
                            document_id=self.id, session_id=self.session_id,
                            source="core-action")
        self._client.bus.publish(event)

    # -- selection (live, auto-waiting) --------------------------------------
    def _nth(self, locator: Any, selector: str, index: int,
             optional: bool) -> LiveNode | None:
        async def resolve() -> Any:
            count = await locator.count()
            if count == 0 and not optional:
                try:
                    await locator.first.wait_for(
                        state="attached",
                        timeout=_timeout_ms(None, self._client.timeout))
                    count = await locator.count()
                except Exception:
                    raise LookupError(f"no match for {selector!r}") from None
            position = index if index >= 0 else count + index
            if position < 0 or position >= count:
                if optional:
                    return None
                raise LookupError(
                    f"no match for {selector!r} at index {index} "
                    f"({count} matches)")
            return locator.nth(position)

        found = self._run(resolve())
        return None if found is None else LiveNode._wrap_locator(found, self)

    def _all(self, locator: Any, limit: int | None,
             offset: int) -> Sequence[LiveNode]:
        count = self._run(locator.count())
        stop = count if limit is None else min(count, offset + limit)
        return [LiveNode._wrap_locator(locator.nth(i), self)
                for i in range(offset, stop)]

    def select(self, selector: str, *, index: int = 0,
               optional: bool = False) -> Any:
        return self._nth(self._page.locator(_pw_selector(selector)),
                         selector, index, optional)

    def select_all(self, selector: str, limit: int | None = None,
                   offset: int = 0) -> Sequence[LiveNode]:
        return self._all(self._page.locator(_pw_selector(selector)),
                         limit, offset)

    # -- interactions --------------------------------------------------------
    def _act(self, action: str, *, selector: str | None = None,
             locator: Any = None, text: str | None = None,
             button: str = "left", count: int = 1,
             delay_ms: int | None = None, clear: bool = True,
             checked: bool = True, value: Any = None, label: Any = None,
             index: Any = None, files: Sequence[str] | None = None,
             target: str | None = None, key: str | None = None,
             timeout: float | None = None, optional: bool = False) -> None:
        page = self._page
        if page is None:
            raise RuntimeError(
                "this LiveDocument was released or navigated away")
        ms = _timeout_ms(timeout, self._client.timeout)
        loc: Any = locator if locator is not None else (
            page.locator(_pw_selector(selector)) if selector else None)

        async def perform() -> None:
            if action == "click":
                await loc.first.click(button=button, click_count=count, timeout=ms)
            elif action == "write":
                if clear:
                    await loc.first.fill(text or "", timeout=ms)
                else:
                    await loc.first.press_sequentially(
                        text or "", delay=delay_ms, timeout=ms)
            elif action == "press":
                if loc is not None:
                    await loc.first.press(key, timeout=ms)
                else:
                    await page.keyboard.press(key)
            elif action == "hover":
                await loc.first.hover(timeout=ms)
            elif action == "check":
                await (loc.first.check(timeout=ms) if checked
                       else loc.first.uncheck(timeout=ms))
            elif action == "select_option":
                await loc.first.select_option(
                    value=value, label=label, index=index, timeout=ms)
            elif action == "upload":
                await loc.first.set_input_files(list(files or []), timeout=ms)
            elif action == "drag":
                await page.drag_and_drop(
                    _pw_selector(selector or ""), _pw_selector(target or ""),
                    timeout=ms)
            elif action == "scroll":
                if loc is not None:
                    await loc.first.evaluate(
                        "(el, d) => el.scrollBy(d.x, d.y)",
                        {"x": value or 0, "y": index or 0})
                else:
                    await page.evaluate(
                        "d => window.scrollBy(d.x, d.y)",
                        {"x": value or 0, "y": index or 0})

        self._emit_action(action, selector=selector, text=text, key=key,
                          target=target)
        try:
            self._run(perform())
        except LookupError:
            raise
        except Exception as exc:
            if optional and "Timeout" in type(exc).__name__:
                return
            if "Timeout" in type(exc).__name__:
                raise LookupError(
                    f"{action}: no target for {selector!r} within "
                    f"{ms / 1000:.1f}s") from exc
            raise

    def click(self, selector: str, *, button: Literal["left", "middle", "right"] = "left",
              count: int = 1, timeout: float | None = None,
              optional: bool = False) -> Self:
        self._act("click", selector=selector, button=button, count=count,
                  timeout=timeout, optional=optional)
        return self

    def write(self, selector: str, text: str, *, clear: bool = True,
              delay_ms: int | None = None, timeout: float | None = None,
              optional: bool = False) -> Self:
        self._act("write", selector=selector, text=text, clear=clear,
                  delay_ms=delay_ms, timeout=timeout, optional=optional)
        return self

    def press(self, key: str, *, selector: str | None = None,
              timeout: float | None = None) -> Self:
        self._act("press", selector=selector, key=key, timeout=timeout)
        return self

    def hover(self, selector: str, *, timeout: float | None = None,
              optional: bool = False) -> Self:
        self._act("hover", selector=selector, timeout=timeout,
                  optional=optional)
        return self

    def check(self, selector: str, checked: bool = True, *,
              timeout: float | None = None) -> Self:
        self._act("check", selector=selector, checked=checked, timeout=timeout)
        return self

    def select_option(self, selector: str, *, value: str | None = None,
                      label: str | None = None,
                      index: int | None = None) -> Self:
        self._act("select_option", selector=selector, value=value,
                  label=label, index=index)
        return self

    def upload(self, selector: str, files: Sequence[str]) -> Self:
        self._act("upload", selector=selector, files=files)
        return self

    def drag(self, source: str, target: str) -> Self:
        self._act("drag", selector=source, target=target)
        return self

    def scroll(self, selector: str | None = None, *, x: int = 0,
               y: int = 0) -> Self:
        self._act("scroll", selector=selector, value=x, index=y)
        return self

    # -- scripting -----------------------------------------------------------
    def execute(self, script: str) -> Self:
        self._emit_action("execute", script=script[:200])
        self._run(self._page.evaluate(script))
        return self

    def evaluate(self, script: str) -> Any:
        return self._run(self._page.evaluate(script))

    def screenshot(self, selector: str | None = None, *,
                   full_page: bool = False,
                   format: Literal["png", "jpeg"] = "png") -> BinaryDocument:
        if selector is not None:
            node = self.select(selector)
            return self._screenshot_of(node._locator, format=format)
        data = self._run(self._page.screenshot(full_page=full_page, type=format))
        return self._binary(data, format)

    def _screenshot_of(self, locator: Any,
                       format: Literal["png", "jpeg"] = "png") -> BinaryDocument:
        return self._binary(self._run(locator.screenshot(type=format)), format)

    def _binary(self, data: bytes, format: str) -> BinaryDocument:
        doc = BinaryDocument(hostname=self.hostname, scheme=self.scheme,
                             path=self.path, content=data, status_code=200,
                             media_type=f"image/{format}")
        doc._client = self._client
        return doc

    # -- waiting -------------------------------------------------------------
    def wait_for(self, selector: str | None = None, *,
                 event: str | None = None,
                 state: Literal["attached", "visible", "hidden", "detached"] = "visible",
                 timeout: float | None = None,
                 optional: bool = False) -> Self:
        if selector is not None:
            ms = _timeout_ms(timeout, self._client.timeout)
            try:
                self._run(self._page.wait_for_selector(
                    _pw_selector(selector), state=state, timeout=ms))
            except Exception as exc:
                if optional and "Timeout" in type(exc).__name__:
                    return self
                raise LookupError(
                    f"wait_for: {selector!r} not {state} within "
                    f"{ms / 1000:.1f}s") from exc
        elif event is not None:
            self._run(self._page.wait_for_load_state(event))
        elif timeout is not None:
            import asyncio

            async def sleep() -> None:
                await asyncio.sleep(timeout)
            self._run(sleep())
        return self

    # -- navigation ----------------------------------------------------------
    def navigate(self, target: "Reference | str", *,
                 headers: dict[str, str] | None = None,
                 wait_until: str = "load") -> "LiveDocument":
        ref = target if isinstance(target, Reference) else self.join(target)
        return self._client._navigate(self, ref, headers=headers,
                                      wait_until=wait_until)

    def back(self) -> "LiveDocument":
        return self._client._history(self, "back")

    def forward(self) -> "LiveDocument":
        return self._client._history(self, "forward")

    # -- recording / events --------------------------------------------------
    def replay(self, actions: Sequence[ActionEvent]) -> Self:
        for event in actions:
            args = {k: v for k, v in event.args.items() if v is not None}
            if event.action == "execute":
                continue               # scripts are not replayed blindly
            self._act(event.action, **args)
        return self

    def subscribe(self, topic: Topic, handler: Any) -> Any:
        return self._client.bus.subscribe(topic, handler,
                                          document_id=self.id)

    # typed views over the routed event store
    @property
    def xhr_requests(self) -> Sequence[XHREvent]:
        return self.events_of(XHREvent)

    @property
    def dom_mutations(self) -> Sequence[DOMUpdateEvent]:
        return self.events_of(DOMUpdateEvent)

    @property
    def console(self) -> Sequence[ConsoleEvent]:
        return self.events_of(ConsoleEvent)

    # -- pagination: action-driven live pagination arrives with the executor -
    def paginate(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("live pagination lands in M6")
