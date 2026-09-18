"""Plan/expr visualization: two read-only renderers over the :class:`Plan` IR.

A recorded ``Expr`` carries a :class:`~webclient.query.plan.Plan` -- an ordered
list of get/call/op/fn/when steps. This module walks that plan (it never runs it)
and renders it two ways:

* :func:`explain` -- a SQL-EXPLAIN-style indented step tree (one op per line, its
  key args and the data flow), for logs / the demo / an LLM reading a plan.
* :func:`wireframe` -- a self-contained HTML string (inline CSS + SVG, no external
  deps, light/dark-neutral) mapping the plan to a visual grammar: a ``resolve``
  becomes a browser-chrome page frame, ``select`` a solid box, ``select_all`` a box
  with a dotted ghost duplicate behind it, ``extract`` field chips, a link-follow
  ``attr("href").resolve()`` an arrow to a nested frame, ``filter`` a predicate
  badge and ``project`` an output card.

Both are pure functions of a ``Plan`` -- siloed from the cores/executor. The thin
``Expr.wireframe()`` / ``Expr.explain()`` methods just call in here.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from .plan import Arg, Plan, Step, _url_from_source

if TYPE_CHECKING:
    from collections.abc import Iterable


# --------------------------------------------------------------------------- #
# grouping: pair get+call steps into ops (the same pass ``describe`` walks, but
# structured so both renderers share one reading of the plan).
# --------------------------------------------------------------------------- #


class _Op:
    """One logical operation lifted from the step list: a method call
    (``get`` + ``call``), a bare property (``get``), an operator, a function or a
    ``when`` branch. ``args``/``kwargs`` are the call's raw :class:`Arg` s."""

    __slots__ = ("name", "args", "kwargs", "kind")

    def __init__(
        self,
        name: str,
        args: "list[Arg]",
        kwargs: "dict[str, Arg]",
        kind: str = "call",
    ) -> None:
        self.name = name
        self.args = args
        self.kwargs = kwargs
        self.kind = kind  # "call" | "prop" | "op" | "fn" | "when"

    @property
    def fans_out(self) -> bool:
        return self.name == "select_all"

    @property
    def follows_link(self) -> bool:
        """A sub-plan that ends in a ``resolve`` -- i.e. an ``attr('href').resolve()``
        style link-follow that opens a NEW page (rendered as a nested frame)."""
        for arg in (*self.args, *self.kwargs.values()):
            if arg.plan is not None and _has_resolve(arg.plan):
                return True
        return False


def _group(steps: "Iterable[Step]") -> "list[_Op]":
    steps = list(steps)
    ops: list[_Op] = []
    i, n = 0, len(steps)
    while i < n:
        s = steps[i]
        if s.kind == "get":
            nxt = steps[i + 1] if i + 1 < n else None
            if nxt is not None and nxt.kind == "call":
                ops.append(_Op(s.name, nxt.args, nxt.kwargs, "call"))
                i += 2
                continue
            ops.append(_Op(s.name, [], {}, "prop"))
        elif s.kind == "op":
            ops.append(_Op(s.name, s.args, {}, "op"))
        elif s.kind == "fn":
            ops.append(_Op(s.name, s.args, s.kwargs, "fn"))
        elif s.kind == "when":
            ops.append(_Op("when", s.args, s.kwargs, "when"))
        i += 1
    return ops


def _has_resolve(plan: Plan) -> bool:
    return any(st.kind == "get" and st.name == "resolve" for st in plan.steps)


def _root_label(plan: Plan) -> str:
    if plan.source is not None:
        return f"reference({_url_from_source(plan.source)!r})"
    return plan.root or "_"


def _sub_desc(plan: Plan) -> str:
    """A compact rendering of a sub-plan (an extract column / predicate): the
    canonical ``describe`` form with a leading root token stripped, so
    ``wq.doc.select('h3').attr('text')`` reads as ``select('h3').attr('text')``."""
    text = plan.describe()
    for pre in ("Document.", "Reference.", "Collection.", "Field.", "_."):
        if text.startswith(pre):
            return text[len(pre) :]
    return text


def _val(arg: Arg) -> str:
    if arg.plan is not None:
        return _sub_desc(arg.plan)
    v = arg.value
    return v if isinstance(v, str) else repr(v)


# --------------------------------------------------------------------------- #
# explain: the indented step tree
# --------------------------------------------------------------------------- #

#: op name -> its Python symbol, for the ``op`` steps rendered as a predicate.
_OP_SYM = {
    "eq": "==", "ne": "!=", "lt": "<", "le": "<=", "gt": ">", "ge": ">=",
    "and": "&", "or": "|", "not": "~",
}


def _op_line(op: _Op) -> str:
    """One op's headline: ``NAME  positional  [kw=…]`` plus an annotation."""
    if op.kind == "op":
        sym = _OP_SYM.get(op.name, op.name)
        rhs = _val(op.args[0]) if op.args else ""
        return f"{sym} {rhs}".strip() if op.name != "not" else "~ (not)"
    if op.name == "extract":
        return "EXTRACT"  # its columns hang below as field branches
    if op.name == "project":
        return "PROJECT → rows"
    if op.name == "filter":
        preds = ", ".join(_val(a) for a in op.args)
        return f"FILTER  [{preds}]"
    name = op.name.upper()
    pos = [_val(a) for a in op.args]
    kw = [f"{k}={_val(v)}" for k, v in op.kwargs.items()]
    seg = name
    label = "  ".join(p for p in pos if p)
    if label:
        seg += f"  {label}"
    if kw:
        seg += f"  [{', '.join(kw)}]"
    if op.fans_out:
        seg += "  (fan-out)"
    return seg


def explain(plan: Plan) -> str:
    """A SQL-EXPLAIN-style indented step tree for ``plan`` (a string). Each op is
    one line with its key args; the pipeline is a spine (data flows downward) and
    an ``extract``'s columns hang below it as field branches (recursing into a
    link-follow sub-plan). Read-only -- it walks the plan, never runs it."""
    lines = [_root_label(plan)]
    depth = 0
    for op in _group(plan.steps):
        pad = "   " * depth
        lines.append(f"{pad}└─ {_op_line(op)}")
        if op.name == "extract":
            fpad = "   " * (depth + 1)
            for key, arg in op.kwargs.items():
                lines.append(f"{fpad}├─ {key} = {_val(arg)}")
                if arg.plan is not None and _has_resolve(arg.plan):
                    lines.append(f"{fpad}│  └─ RESOLVE (per row) → detail page")
        depth += 1
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# wireframe: a self-contained HTML string (inline CSS/SVG, light/dark-neutral)
# --------------------------------------------------------------------------- #

_CSS = """
:root {
  --wc-bg: #ffffff; --wc-fg: #1a1a1a; --wc-muted: #6b7280;
  --wc-line: #9ca3af; --wc-box: #f3f4f6; --wc-chrome: #e5e7eb;
  --wc-accent: #3b82f6; --wc-chip: #eef2ff; --wc-out: #ecfdf5;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --wc-bg: #111317; --wc-fg: #e5e7eb; --wc-muted: #9ca3af;
    --wc-line: #4b5563; --wc-box: #1f2937; --wc-chrome: #374151;
    --wc-accent: #60a5fa; --wc-chip: #1e293b; --wc-out: #052e26;
  }
}
:root[data-theme="dark"] {
  --wc-bg: #111317; --wc-fg: #e5e7eb; --wc-muted: #9ca3af;
  --wc-line: #4b5563; --wc-box: #1f2937; --wc-chrome: #374151;
  --wc-accent: #60a5fa; --wc-chip: #1e293b; --wc-out: #052e26;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px 16px; background: var(--wc-bg); color: var(--wc-fg);
  font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
}
.wc-pipe { display: flex; flex-direction: column; gap: 14px; max-width: 640px; }
.wc-frame { border: 1px solid var(--wc-line); border-radius: 10px; overflow: hidden;
  background: var(--wc-bg); }
.wc-chrome { display: flex; align-items: center; gap: 6px; padding: 8px 12px;
  background: var(--wc-chrome); border-bottom: 1px solid var(--wc-line); }
.wc-dot { width: 10px; height: 10px; border-radius: 50%; background: var(--wc-line); }
.wc-src { margin-left: 6px; color: var(--wc-fg); font-weight: 600;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wc-body { padding: 16px; display: flex; flex-direction: column; gap: 12px; }
.wc-box { position: relative; padding: 10px 12px; border-radius: 8px;
  border: 1.5px solid var(--wc-accent); background: var(--wc-box); }
.wc-box .wc-label { color: var(--wc-muted); font-size: 12px; }
.wc-box .wc-sel { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.wc-selectall { position: relative; }
.wc-ghost { position: absolute; inset: 6px -6px -6px 6px; border-radius: 8px;
  border: 1.5px dashed var(--wc-line); background: transparent; z-index: 0; }
.wc-selectall > .wc-face { position: relative; z-index: 1; }
.wc-many { color: var(--wc-muted); font-size: 12px; }
.wc-chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
.wc-chip { display: inline-flex; flex-direction: column; padding: 6px 10px;
  border-radius: 999px; background: var(--wc-chip); border: 1px solid var(--wc-line); }
.wc-chip b { font-size: 13px; }
.wc-chip small { color: var(--wc-muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.wc-badge { align-self: flex-start; padding: 4px 10px; border-radius: 6px;
  background: var(--wc-chip); border: 1px dashed var(--wc-line); color: var(--wc-muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.wc-output { padding: 10px 12px; border-radius: 8px; border: 1.5px solid #10b981;
  background: var(--wc-out); font-weight: 600; }
.wc-arrow { display: flex; align-items: center; gap: 8px; color: var(--wc-muted);
  font-size: 12px; }
.wc-nested { margin-top: 8px; }
""".strip()

_ARROW_SVG = (
    '<svg width="28" height="14" viewBox="0 0 28 14" fill="none" '
    'xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
    '<path d="M1 7h22" stroke="var(--wc-line)" stroke-width="1.5"/>'
    '<path d="M20 2l6 5-6 5" stroke="var(--wc-line)" stroke-width="1.5" '
    'fill="none"/></svg>'
)


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _tail_selector(plan: Plan) -> str:
    """A compact selector label for a link-follow field's sub-plan (its first
    ``select`` argument, if any) -- what the nested frame is titled with."""
    ops = _group(plan.steps)
    for op in ops:
        if op.name in ("select", "select_all") and op.args:
            return _val(op.args[0])
    return _sub_desc(plan)


def _frame(src: str, body: str) -> str:
    return (
        '<div class="wc-frame"><div class="wc-chrome">'
        '<span class="wc-dot"></span><span class="wc-dot"></span>'
        '<span class="wc-dot"></span>'
        f'<span class="wc-src">{_esc(src)}</span></div>'
        f'<div class="wc-body">{body}</div></div>'
    )


def _box(op: _Op) -> str:
    """A ``select`` / ``select_all`` node -> a solid box (with a ghost duplicate
    behind it for the fan-out select_all)."""
    sel = _val(op.args[0]) if op.args else ""
    verb = "select_all" if op.fans_out else "select"
    face = (
        f'<div class="wc-face"><span class="wc-label">{verb}</span> '
        f'<span class="wc-sel">{_esc(sel)}</span>'
    )
    if op.fans_out:
        face += ' <span class="wc-many">× many</span>'
    face += "</div>"
    if op.fans_out:
        return (
            '<div class="wc-box wc-selectall">'
            '<div class="wc-ghost"></div>' + face + "</div>"
        )
    return f'<div class="wc-box wc-select">{face}</div>'


def _chips(op: _Op) -> str:
    """An ``extract`` node -> field chips (name + sub-selector), pinned in the
    container; a link-follow column also emits an arrow to a nested frame."""
    chips: list[str] = []
    nested: list[str] = []
    for key, arg in op.kwargs.items():
        sub = _val(arg)
        chips.append(
            f'<span class="wc-chip"><b>{_esc(key)}</b>'
            f'<small>{_esc(sub)}</small></span>'
        )
        if arg.plan is not None and _has_resolve(arg.plan):
            nested.append(
                '<div class="wc-nested"><div class="wc-arrow">'
                f'{_ARROW_SVG}<span>{_esc(key)} follows a link</span></div>'
                + _frame(_tail_selector(arg.plan), "") + "</div>"
            )
    body = (
        '<div class="wc-box"><span class="wc-label">extract</span>'
        f'<div class="wc-chips">{"".join(chips)}</div></div>'
    )
    return body + "".join(nested)


def _node(op: _Op) -> str:
    if op.name in ("select", "select_all"):
        return _box(op)
    if op.name == "extract":
        return _chips(op)
    if op.name == "filter":
        preds = ", ".join(_val(a) for a in op.args)
        return f'<div class="wc-badge">filter: {_esc(preds)}</div>'
    if op.name == "project":
        return '<div class="wc-output">project → rows</div>'
    if op.name == "resolve":
        return ""  # the frame itself is the resolve
    # any other op (a step action, a value op, a property) -> a plain labelled box
    label = _op_line(op)
    return f'<div class="wc-badge">{_esc(label)}</div>'


def _pipeline_html(plan: Plan) -> str:
    """The plan's inner pipeline (everything after the root frame's resolve)."""
    ops = _group(plan.steps)
    # the root frame's source: reference(url) or the browser= of the first resolve
    src = _root_label(plan)
    first_resolve = next((o for o in ops if o.name == "resolve"), None)
    if first_resolve is not None and plan.source is None:
        browser = first_resolve.kwargs.get("browser")
        src = f"resolve(browser={_val(browser)})" if browser is not None else "resolve()"
    body_nodes = [n for n in (_node(o) for o in ops) if n]
    frame = _frame(src, "".join(body_nodes))
    return f'<div class="wc-pipe">{frame}</div>'


def wireframe(plan: Plan) -> str:
    """A self-contained HTML page (inline CSS + SVG, no external deps,
    light/dark-neutral) picturing ``plan``: the ``resolve`` root is a
    browser-chrome page frame; ``select`` a solid box, ``select_all`` a box with a
    dotted ghost duplicate (× many); ``extract`` field chips; a link-follow
    ``attr('href').resolve()`` an arrow to a nested frame; ``filter`` a predicate
    badge and ``project`` an output card. Read-only over the plan."""
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Plan wireframe</title>"
        f"<style>{_CSS}</style></head><body>"
        f"{_pipeline_html(plan)}"
        "</body></html>"
    )


__all__ = ["explain", "wireframe"]
