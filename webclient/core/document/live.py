"""Live page backing (M4): interaction on a real browser page.

A live ``Document`` carries a playwright ``_page``; this backing provides
the interaction set (``click`` / ``write`` / ``wait_for``), live selection, and
``reload``. Every op runs on the engine loop (where the browser lives) and
bridges back synchronously, so the eager surface stays sync. Console messages
and DOM mutations are captured onto the document as events (so ``console`` /
``dom_mutations`` / ``events_of`` and per-element narrowing work).

This backing owns the *whole* browser-capture concern: the injected JS
(``INIT_JS`` / ``DRAIN_JS``, declared as its ``page_scripts``), and the helpers
that turn raw page signals into events -- ``drain`` (DOM mutations), plus
``on_load`` wrapping the load-time console/network the client hands back. The
client just leases a page and fires ``on_load``; it never shapes events itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from ...clients import PageScript
from ...models import ActionEvent, ConsoleEvent, DOMUpdateEvent, NetworkEvent
from ..web_core import Backing

if TYPE_CHECKING:
    from ...clients import PageResult
    from ..client.loop import EngineLoop
    from . import Document
    from .html import HtmlBacking

#: installed on every navigation -- an id-path-tagging MutationObserver feeding
#: ``window.__wc_mutations`` (see ``LiveBacking.page_scripts``).
INIT_JS = """(() => {
  if (window.__wc_installed) return;
  window.__wc_installed = true;
  window.__wc_mutations = [];
  // --- correlation substrate: an append-only XHR timeline + a SEPARATE, append-only
  // stamp stream (never overwritten, even between drains). Node identity is a set-once
  // data-wc-node attribute (an internal stamp -- stripped from skeleton/output, never a
  // selector). See webclient/core/document/correlate.py.
  window.__wc_xhr = window.__wc_xhr || [];         // {index, method, url, t} (t = secs since first)
  window.__wc_xhr_index = window.__wc_xhr_index || 0;  // COMPLETED xhr/fetch count (the xhr phase)
  window.__wc_action_index = window.__wc_action_index || 0;  // interactions performed (the action phase)
  window.__wc_node_seq = window.__wc_node_seq || 0;    // set-once node-identity counter
  window.__wc_t0 = (typeof window.__wc_t0 === 'number') ? window.__wc_t0 : null;
  window.__wc_stamps = window.__wc_stamps || [];   // {node, xhr, action, t} -- emitted SEPARATELY
  const relSecs = () => window.__wc_t0 == null ? 0 : (performance.now() - window.__wc_t0) / 1000;
  const startReq = () => { if (window.__wc_t0 == null) window.__wc_t0 = performance.now(); };
  const doneReq = (method, url) => {
    try { url = new URL(url || '', location.href).href; } catch (e) { url = String(url || ''); }
    window.__wc_xhr_index += 1;
    if (window.__wc_xhr.length < 500)
      window.__wc_xhr.push({index: window.__wc_xhr_index, method: (method || 'GET'),
                            url: url, t: relSecs()});
  };
  const stamp = (el) => {  // set-once identity; return the stamp id (null for non-elements)
    if (!el || el.nodeType !== 1) return null;
    if (!el.hasAttribute('data-wc-node'))
      el.setAttribute('data-wc-node', 'n' + (++window.__wc_node_seq));
    return el.getAttribute('data-wc-node');
  };
  const emitStamp = (el) => {  // append-only -> a re-render appends, never overwrites
    const node = stamp(el);
    if (node && window.__wc_stamps.length < 8000) {
      // a bounded text snippet of the node -- the content-matching Correlator scores it against
      // the XHR response bodies. textContent (NOT innerText: innerText forces a synchronous
      // reflow on every mutation, which is costly on a busy SPA); clipped.
      let txt = '';
      try { txt = (el.textContent || '').slice(0, 200); } catch (e) {}
      window.__wc_stamps.push({node: node, xhr: window.__wc_xhr_index,
                               action: window.__wc_action_index, t: relSecs(), text: txt});
    }
  };
  // wrap fetch + XMLHttpRequest so completion bumps the phase counter + records the request.
  const _fetch = window.fetch;
  if (_fetch && !_fetch.__wc) {
    const w = function(input, init) {
      const url = (typeof input === 'string') ? input : (input && input.url) || '';
      const method = (init && init.method) || (input && input.method) || 'GET';
      startReq();
      return _fetch.apply(this, arguments).then(
        (r) => { doneReq(method, url); return r; },
        (e) => { doneReq(method, url); throw e; });
    };
    w.__wc = true; window.fetch = w;
  }
  const _open = XMLHttpRequest.prototype.open, _send = XMLHttpRequest.prototype.send;
  if (_open && !_open.__wc) {
    XMLHttpRequest.prototype.open = function(m, u) { this.__wc_m = m; this.__wc_u = u; return _open.apply(this, arguments); };
    XMLHttpRequest.prototype.open.__wc = true;
    XMLHttpRequest.prototype.send = function() {
      startReq();
      try { this.addEventListener('loadend', () => doneReq(this.__wc_m, this.__wc_u)); } catch (e) {}
      return _send.apply(this, arguments);
    };
  }
  // --- interactivity (System B, dynamic tier): stamp data-wc-int on elements that get an
  // interactive listener, so a <div> made clickable in JS is visible. Rides the DOM snapshot
  // (internal, stripped from output, never a selector). The static/semantic tier + a
  // cursor:pointer scan (below) OR this together give a robust "clickable" signal.
  const _INT = {click:'click', mousedown:'click', pointerdown:'click', keydown:'click',
                mouseenter:'hover', mouseover:'hover', pointerenter:'hover',
                scroll:'scroll', wheel:'scroll'};
  const addInt = (el, k) => {  // accumulate a kind on data-wc-int (space-separated, deduped)
    if (!el || el.nodeType !== 1) return;
    const cur = el.getAttribute('data-wc-int') || '';
    if (cur.split(' ').indexOf(k) < 0) el.setAttribute('data-wc-int', (cur ? cur + ' ' : '') + k);
  };
  const _addEL = EventTarget.prototype.addEventListener;
  if (_addEL && !_addEL.__wc) {
    const wrapped = function(type, listener, opts) {
      try { const k = _INT[type]; if (k && this instanceof Element) addInt(this, k); } catch (e) {}
      return _addEL.apply(this, arguments);
    };
    wrapped.__wc = true;
    EventTarget.prototype.addEventListener = wrapped;
  }
  // A capture-time scan: cursor:pointer marks a click affordance EVEN under event delegation
  // (React attaches one listener at the root, so the wrap above misses the real target); a
  // scrollable-overflow container is a scroll target. Bounded so a huge page stays cheap.
  window.__wc_scan_int = () => {
    let n = 0;
    for (const el of document.querySelectorAll('div,span,li,td,th,section,article,a,label,summary,p,i')) {
      if (n++ > 3000) break;
      try {
        const cs = getComputedStyle(el);
        if (cs.cursor === 'pointer') addInt(el, 'click');
        const oy = cs.overflowY;
        if ((oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight + 4) addInt(el, 'scroll');
      } catch (e) {}
    }
    return n;
  };
  const MAIN = 'MAIN,ARTICLE,SECTION';
  new MutationObserver((muts) => {
    for (const m of muts) {
      let ids = []; let n = m.target; let inMain = false;
      while (n && n.nodeType === 1) {
        if (n.id) ids.push(n.id);
        if (MAIN.indexOf(n.tagName) >= 0 ||
            (n.getAttribute && n.getAttribute('role') === 'main')) inMain = true;
        n = n.parentElement;
      }
      window.__wc_mutations.push({type: m.type, ids: ids, inMain: inMain,
        added: m.addedNodes.length, removed: m.removedNodes.length});
      // emit phase stamps SEPARATELY: the mutated target + any added element nodes, so a
      // record region is attributed to the request(s) that had completed by this mutation.
      emitStamp(m.target);
      for (const an of m.addedNodes) emitStamp(an);
    }
  }).observe(document,
             {childList: true, subtree: true, attributes: true, characterData: true});
  // At DOMContentLoaded (the served HTML parsed), discard the parse mutations and
  // record the text length THEN -- the baseline. Post-load injection is measured as
  // NET text GROWTH over it, so re-organising existing DOM (re-adds nodes but adds
  // no new text) is not mistaken for client-side rendering. (Net growth catches
  // shell-style SPAs; framework markers catch transform-style ones like AEM Edge,
  // whose text is replaced rather than grown.)
  const mark = () => {
    window.__wc_mutations = [];
    window.__wc_stamps = [];  // discard pre-DCL (parse/static) stamps, like the parse mutations
    window.__wc_dcl_text = document.body ? (document.body.innerText || '').length : 0;
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mark, {once: true});
  } else { mark(); }
  // Fold shadow-DOM + same-origin iframe content into the LIGHT dom so page.content()
  // (and the skeleton the agent reads) contains the real records, not an empty
  // <custom-element>/<iframe> shell. Called from the "inline" page-script after settle.
  // Recurse into nested shadow roots first so deep trees are captured too.
  window.__wc_inline = () => {
    let shadow = 0, frames = 0;
    const pierce = (root) => {
      let n = 0;
      const hosts = root.querySelectorAll('*');
      for (const el of hosts) {
        if (el.shadowRoot) {
          n += 1 + pierce(el.shadowRoot);            // nested shadows first
          const holder = document.createElement('div');
          holder.setAttribute('data-wc-shadow', '');
          holder.innerHTML = el.shadowRoot.innerHTML;  // now includes inlined descendants
          el.appendChild(holder);
        }
      }
      return n;
    };
    try { shadow = pierce(document); } catch (e) {}
    for (const f of document.querySelectorAll('iframe')) {
      try {
        const idoc = f.contentDocument;               // null / throws for cross-origin
        if (idoc && idoc.body) {
          frames++;
          const holder = document.createElement('div');
          holder.setAttribute('data-wc-frame', f.getAttribute('src') || '');
          holder.innerHTML = idoc.body.innerHTML;
          f.parentNode.insertBefore(holder, f.nextSibling);
        }
      } catch (e) { /* cross-origin frame -- unreadable, leave it */ }
    }
    return {shadow: shadow, frames: frames};
  };
})()"""

#: read + clear the mutation buffer AND snapshot the settled page's total text +
#: element count + the DOMContentLoaded-baseline text (``dclText``) -- the runtime
#: facet measures injection as the NET text grown past that baseline. Run after
#: replay to discard load noise; on load-time capture the caller keeps the result
#: (see ``clients.browser.open``).
DRAIN_JS = """() => {
  const m = window.__wc_mutations || []; window.__wc_mutations = [];
  const s = window.__wc_stamps || []; window.__wc_stamps = [];  // the append-only phase stream
  const t = document.body ? (document.body.innerText || '').length : 0;
  return {muts: m, stamps: s, xhr: (window.__wc_xhr || []),
          text: t, nodes: document.getElementsByTagName('*').length,
          dclText: window.__wc_dcl_text || 0};
}"""

_LEVELS = {
    "log": "log",
    "info": "info",
    "debug": "log",
    "warning": "warning",
    "error": "error",
}


def _kind(record: dict[str, Any]) -> str:
    if record["type"] == "attributes":
        return "attribute"
    if record["type"] == "characterData":
        return "text"
    return "removed" if record["removed"] and not record["added"] else "added"


def _is_timeout(exc: BaseException) -> bool:
    """Whether ``exc`` is a Playwright timeout (its many timeout classes all end in
    ``TimeoutError``) -- the one place the stringly-typed heuristic lives."""
    return "Timeout" in type(exc).__name__


def _mutation_event(r: dict[str, Any], doc: "Document", *, phase: str | None = None) -> DOMUpdateEvent:
    """Wrap one raw mutation record into a DOMUpdateEvent, carrying the position /
    size detail the runtime facet reads (``inMain`` / ``added`` / ``addedText``)."""
    detail: dict[str, Any] = {
        "ids": r.get("ids", []),
        "inMain": bool(r.get("inMain")),
        "added": int(r.get("added", 0)),
    }
    if phase:
        detail["phase"] = phase
    return DOMUpdateEvent(kind=cast(Any, _kind(r)), detail=detail, document_id=doc.name)


async def drain(doc: "Document") -> None:
    """After an interaction: move any pending DOM mutations onto the document AND
    refresh its captured ``content`` from the (now-changed) live page, so a later
    ``select`` / ``text_content`` / ``skeleton`` -- including the in-memory fallback
    the plan evaluator takes on the engine loop -- sees the post-interaction DOM, not
    the original render. A short settle lets the observer deliver the last records."""
    await doc._page.wait_for_timeout(30)
    result = await doc._page.evaluate(DRAIN_JS)
    muts = result.get("muts", []) if isinstance(result, dict) else result
    for r in muts:
        doc._events.append(_mutation_event(r, doc))
    if isinstance(result, dict):  # refresh the correlation substrate after the interaction
        doc._stamps.extend(result.get("stamps", []))
        # the xhr timeline is cumulative -- replace this doc's correlated NetworkEvents
        doc._events = [e for e in doc._events if not (isinstance(e, NetworkEvent) and e.index is not None)]
        doc._events.extend(xhr_events(result.get("xhr", []), doc, doc._xhr_bodies))
    doc.content = (await doc._page.content()).encode()  # keep content current
    doc._tree = None  # invalidate the cached lxml parse of the old content


def console_event(level: str, text: str, doc: "Document") -> ConsoleEvent:
    return ConsoleEvent(
        level=cast(Any, _LEVELS.get(level, "log")), text=text, document_id=doc.name
    )


def network_event(method: str, url: str, resource_type: str, doc: "Document") -> NetworkEvent:
    """A browser sub-request captured onto the document (an XHR/fetch the page
    made) -- the raw material for the summary ``runtime`` facet's xhr_endpoints."""
    from ..reference import from_url

    return NetworkEvent(
        request=from_url(url, cast(Any, method.lower())),
        resource_type=resource_type,
        document_id=doc.name,
    )


def xhr_events(
    xhr: "list[dict[str, Any]]", doc: "Document", bodies: "dict[str, list[str]] | None" = None
) -> "list[NetworkEvent]":
    """The correlated XHR timeline (from the fetch/XHR wrapper) -> NetworkEvents carrying a
    completion ``index`` + relative ``t_s``, the raw material the :class:`Correlator` reads.

    ``bodies`` (url -> response texts in order) populates ``NetworkEvent.body`` so the
    content-matching :class:`ContentCorrelator` can value-match it against node text. Same-url
    requests consume their bodies IN ORDER (a repeat call doesn't clobber the earlier body).
    Absent/unmatched -> ``body`` stays ``None`` (the ordering baseline holds)."""
    from ..reference import from_url

    pools = {k: list(v) for k, v in (bodies or {}).items()}  # copy; consumed per-url, in order
    cursors: dict[str, int] = {}
    out: list[NetworkEvent] = []
    for r in xhr:
        method = str(r.get("method") or "GET")
        url = str(r.get("url") or "")
        raw_index = r.get("index")
        lst = pools.get(url) or []
        i = cursors.get(url, 0)
        cursors[url] = i + 1
        text = lst[i] if i < len(lst) else None
        out.append(
            NetworkEvent(
                request=from_url(url, cast(Any, method.lower())),
                resource_type="xhr",
                method=method,
                index=int(raw_index) if raw_index is not None else None,
                t_s=float(r.get("t") or 0.0),
                body=text.encode("utf-8", "replace") if text is not None else None,
                document_id=doc.name,
            )
        )
    return out


def _html() -> "HtmlBacking":
    """The (stateless) HTML backing, for an in-memory select on captured content."""
    from .html import HtmlBacking

    return HtmlBacking()


class LiveBacking(Backing):
    """Interaction + live selection on a browser page (capability ``page``)."""

    provides = frozenset(
        {"click", "write", "wait_for", "select", "select_all", "evaluate", "screenshot"}
    )
    collections = frozenset({"select_all"})
    props = frozenset({"dom_mutations", "console"})
    #: the always-IO browser interactions -> awaitable under async. ``select`` /
    #: ``select_all`` are omitted: on a *static* document (the common case) they
    #: are in-memory (HtmlBacking), so the surface types them synchronously.
    io = frozenset({"click", "write", "wait_for", "evaluate", "screenshot"})
    #: the browser scripts this backing owns: the mutation observer (``init``,
    #: read by ``dom_mutations`` via ``drain``) and the buffer drain (``drain``
    #: phase, run after replay to discard load-time mutations). The client gathers
    #: and installs them; the backing owns the *what*.
    page_scripts = (
        PageScript(INIT_JS, "init"),
        # fold shadow-DOM / same-origin iframe content into the light DOM before the snapshot
        PageScript("() => window.__wc_inline ? window.__wc_inline() : {shadow:0,frames:0}", "inline"),
        # stamp data-wc-int for cursor:pointer / scrollable elements (dynamic interactivity)
        PageScript("() => window.__wc_scan_int ? window.__wc_scan_int() : 0", "inline"),
        PageScript(DRAIN_JS, "drain"),
    )
    gate = "page"

    def applies(self, core: "Document") -> bool:
        return core._page is not None

    def on_load(self, core: "Document", result: "PageResult") -> None:
        """Wrap the load-time console/network/DOM-mutations the client captured (a
        ``clients.PageResult``) into events on the document -- the client hands
        back raw facts and fires this; the backing owns the shaping."""
        for level, text in result.console:
            core._events.append(console_event(level, text, core))
        for method, url, rtype in result.network:  # XHR/fetch the page issued
            if rtype in ("xhr", "fetch"):
                core._events.append(network_event(method, url, rtype, core))
        # DOM mutations the page made during load/settle -- tagged phase="load" (with
        # position/size detail) so the runtime facet can measure how much content the
        # page composed after navigation, and where.
        for r in getattr(result, "mutations", []):
            core._events.append(_mutation_event(r, core, phase="load"))
        # correlation substrate: the XHR timeline (as indexed NetworkEvents, bodies attached by
        # URL for content matching) + the append-only phase stamps (kept raw on the doc; the
        # Correlator builds from them). Bodies are stashed so a later ``drain`` can reattach them.
        bodies = dict(getattr(result, "bodies", {}) or {})
        core._xhr_bodies.update(bodies)
        core._events.extend(xhr_events(getattr(result, "xhr", []), core, core._xhr_bodies))
        core._stamps.extend(getattr(result, "stamps", []))
        core._render_stats = getattr(result, "dom_stats", {}) or {}

    def _loop(self, core: "Document") -> "EngineLoop":
        return core._client.loop()

    # -- captured event views ------------------------------------------------
    def dom_mutations(self, core: "Document") -> "list[DOMUpdateEvent]":
        return [e for e in core._events if isinstance(e, DOMUpdateEvent)]

    def console(self, core: "Document") -> "list[ConsoleEvent]":
        return [e for e in core._events if isinstance(e, ConsoleEvent)]

    # -- interactions (IO: async def; the interface bridges via dispatch) -----
    async def click(
        self,
        core: "Document",
        selector: str | None = None,
        *,
        timeout: float | None = None,
        optional: bool = False,
        error: Any = None,
    ) -> "Document":
        from ...errors import lenient

        await self._aact(
            core, "click", selector=selector, timeout=timeout,
            optional=lenient(optional, error),
        )
        return core

    async def write(
        self,
        core: "Document",
        selector: str,
        text: str,
        *,
        timeout: float | None = None,
        optional: bool = False,
        error: Any = None,
    ) -> "Document":
        from ...errors import lenient

        await self._aact(
            core, "write", selector=selector, text=text, timeout=timeout,
            optional=lenient(optional, error),
        )
        return core

    async def wait_for(
        self,
        core: "Document",
        selector: str | None = None,
        *,
        timeout: float | None = None,
        optional: bool = False,
        error: Any = None,
    ) -> "Document":
        from ...errors import lenient, select_error

        optional = lenient(optional, error)
        try:
            await self._await_for(core, selector, timeout)
        except Exception as exc:  # a Playwright timeout -> structured miss (or lenient)
            if _is_timeout(exc):
                if optional:
                    return core
                raise select_error(f"wait_for: no {selector!r} within timeout") from exc
            raise
        return core

    # ``select`` / ``select_all`` are NOT ``io``: on a static document (the common
    # case) they are in-memory (HtmlBacking). On a live page they bridge to the
    # live DOM off the engine loop -- BUT when the caller is already ON the engine
    # loop (a plan run by the evaluator, e.g. ``wc.execute`` of a
    # ``resolve(browser="always").select(...)`` plan) that sync bridge is illegal
    # (it would block the loop on itself). There we fall back to an in-memory select
    # on the captured rendered content -- correct for a resolve->select plan, and it
    # keeps the page for any live interaction ops (which are ``io`` and await fine).
    def select(
        self,
        core: "Document",
        selector: str,
        *,
        index: int = 0,
        optional: bool = False,
        error: Any = None,
    ) -> "Document":
        from ...errors import RETURN

        if self._loop(core).on_loop_thread():
            return _html().select(
                core, selector, index=index, optional=optional, error=error
            )
        return self._loop(core).run(
            self._aselect(core, selector, index, RETURN if optional else error)
        )

    def select_all(
        self, core: "Document", selector: str, *, limit: int | None = None, offset: int = 0
    ) -> "list[Document]":
        if self._loop(core).on_loop_thread():
            return _html().select_all(core, selector, limit=limit, offset=offset)
        return self._loop(core).run(self._aselect_all(core, selector, limit=limit, offset=offset))

    async def evaluate(self, core: "Document", script: str, *, mutates: bool = True) -> Any:
        """Run ``script`` in the live page and return its result. ``mutates`` (default
        True) drains afterward -- refreshing the captured content and invalidating the
        cached tree -- so a later ``text_content`` / ``html`` / ``select`` reflects any
        DOM the script changed. Pass ``mutates=False`` for a pure read to skip the settle."""
        result = await core._page.evaluate(script)
        if mutates:
            await drain(core)
        return result

    async def screenshot(self, core: "Document", selector: str | None = None) -> "Document":
        return await self._ashot(core, selector)

    # -- async bodies --------------------------------------------------------
    async def _aact(
        self,
        core: "Document",
        action: str,
        *,
        selector: str | None = None,
        text: str | None = None,
        timeout: float | None = None,
        optional: bool = False,
    ) -> None:
        ms = (timeout or 30.0) * 1000
        event = ActionEvent(
            action=action,
            args={"selector": selector, "text": text},
            document_id=core.name,
            source="core-action",
        )
        core._client.bus.publish(event)
        core._events.append(event)  # routed onto the document
        if core._ref is not None:
            core._ref.actions.append(
                {
                    "op": action,
                    "args": {
                        k: v
                        for k, v in (("selector", selector), ("text", text))
                        if v is not None
                    },
                }
            )
        # bump the client-side ACTION counter BEFORE acting, so any DOM this interaction
        # reveals is phase-stamped with this action's index (the correlation substrate).
        try:
            await core._page.evaluate("window.__wc_action_index = (window.__wc_action_index||0)+1")
        except Exception:  # noqa: BLE001 - a page without the init script: correlation just skips
            pass
        loc = core._page.locator(selector or "*").first
        try:
            if action == "click":
                await loc.click(timeout=ms)
            elif action == "write":
                await loc.fill(text or "", timeout=ms)
        except Exception as exc:
            if _is_timeout(exc):
                if optional:
                    return
                from ...errors import select_error

                raise select_error(f"{action}: no target for {selector!r}") from exc
            raise
        await drain(core)

    async def _await_for(
        self, core: "Document", selector: str | None, timeout: float | None
    ) -> None:
        if selector is not None:
            await core._page.wait_for_selector(
                selector, timeout=(timeout or 30.0) * 1000
            )
        elif timeout is not None:
            await core._page.wait_for_timeout(timeout * 1000)
        await drain(core)

    async def _ashot(self, core: "Document", selector: str | None) -> "Document":
        from . import Document

        target = core._page if selector is None else core._page.locator(selector).first
        data = await target.screenshot(type="png")
        shot = Document(
            url=core.url, kind="binary", content=data, status_code=core.status_code
        )
        shot._client = core._client
        return shot

    async def _aselect(self, core: "Document", selector: str, index: int, error: Any) -> "Document":
        from ...errors import RETURN
        from . import Document

        from ...errors import select_error

        loc = core._page.locator(selector)
        if await loc.count() <= index:
            if error is not RETURN:  # live select is loud by default
                raise select_error(f"no match for {selector!r}")
            sub = Document(url=core.url, kind="html", status_code=core.status_code)
            sub._client = core._client
            sub._missing = True
            return sub
        html = await loc.nth(index).evaluate("el => el.outerHTML")
        sub = Document(
            url=core.url,
            final_url=core.final_url,
            kind="html",
            content=html.encode(),
            status_code=core.status_code,
        )
        sub._client = core._client
        sub.root = core.name
        nid = selector[1:] if selector.startswith("#") and " " not in selector else None
        sub._events = [
            e
            for e in core._events
            if isinstance(e, DOMUpdateEvent)
            # load-time composition is a document-level SPA signal, not an
            # interaction on this element -- exclude it from per-element narrowing.
            and e.detail.get("phase") != "load"
            and (nid is None or nid in e.detail.get("ids", []))
        ]
        return sub

    async def _aselect_all(
        self, core: "Document", selector: str, *, limit: int | None = None, offset: int = 0
    ) -> "list[Document]":
        from . import Document

        loc = core._page.locator(selector)
        total = await loc.count()
        stop = total if limit is None else min(total, offset + limit)
        out: "list[Document]" = []
        for i in range(offset, stop):
            html = await loc.nth(i).evaluate("el => el.outerHTML")
            sub = Document(
                url=core.url,
                final_url=core.final_url,
                kind="html",
                content=html.encode(),
                status_code=core.status_code,
            )
            sub._client = core._client
            sub.root = core.name
            out.append(sub)
        return out


__all__ = [
    "LiveBacking",
    "INIT_JS",
    "DRAIN_JS",
    "drain",
    "console_event",
    "network_event",
]
