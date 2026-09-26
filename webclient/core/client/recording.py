"""What a RECORDING SESSION read, as a plan (everything is a plan).

``rec.plan`` is the recorded JOURNEY: the navigation and the interactions, replayable to the page they
reached. The READS made on that page -- ``select`` / ``select_all`` / ``attr`` / ``text_content`` /
``links``, and a ``resolve`` of a link read off it -- are recorded here as a TREE over the journey's page:

    page ─ select_all("li.card") ─ (each item) ─ select("h3 a") ─ attr("title")
                                              └─ select("a") ─ attr("href") ─ resolve ─ select("p") ─ attr("text")

A collection's items share ONE "each" node (a loop over the cards reads the same things of each), and each
read is stamped with its item's index, so the reads compile to the plan that does the same in one go:

    <journey>.select_all("li.card").extract(h3_a_title=..., p_text=...).project()

Every read runs AS its step: stamped ``@<node>`` (``Event.step``) and publishing the same ``step`` /
``result`` events an executed plan's steps do. The compiled plan's ``steps`` map says where each node landed
(``{"@n3": "4/kw:h3_a_title/2"}``) -- a trace carries it, so a run view places the reads on the plan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_FANS = frozenset({"select_all", "links"})


@dataclass
class RNode:
    id: str
    parent: str | None  # None = the journey's page
    op: str  # an op, or "each" (the item of the collection that is its parent)
    args: tuple[Any, ...] = ()
    kwargs: tuple[tuple[str, Any], ...] = ()
    children: list[str] = field(default_factory=list)
    #: where in the JOURNEY the read was made: after how many of its calls (the page's state then) --
    #: a read before a click is compiled before it
    stage: int = 0


class ReadTree:
    """The reads of one recording session, over its journey's page (reset by a new navigation)."""

    def __init__(self) -> None:
        self.nodes: dict[str, RNode] = {}
        self._objs: dict[int, tuple[str | None, tuple[int, ...]]] = {}
        self._keep: list[Any] = []  # the tracked objects, alive while the session is (ids stay unique)
        self._n = 0

    # -- building ------------------------------------------------------------------
    def reset(self, page: Any) -> None:
        """A navigation: a fresh journey page, nothing read of it yet."""
        self.nodes.clear(); self._objs.clear(); self._keep.clear()
        self.track(page, None, ())

    def track(self, obj: Any, node: str | None, item: tuple[int, ...]) -> None:
        self._objs[id(obj)] = (node, item); self._keep.append(obj)

    def at(self, obj: Any) -> "tuple[str | None, tuple[int, ...]] | None":
        """Where a receiver is in the tree -- (its node, None = the page; its item) -- or None: not recorded."""
        return self._objs.get(id(obj))

    def child(self, parent: str | None, op: str, args: Any = (), kwargs: "dict[str, Any] | None" = None, stage: int = 0) -> str:
        """The node for ``op(args)`` under ``parent`` (the same op on the same node is the same node) -- a read of the
        page itself at ``stage`` of the journey (a read after a click is not the same read as one before it)."""
        a = tuple(x for x in args if isinstance(x, (str, int, float, bool)) or x is None)
        kw = tuple(sorted((k, v) for k, v in (kwargs or {}).items() if isinstance(v, (str, int, float, bool)) or v is None))
        if parent is not None:
            stage = self.nodes[parent].stage
        siblings = self.nodes[parent].children if parent is not None else [n.id for n in self.nodes.values() if n.parent is None]
        for sid in siblings:
            s = self.nodes[sid]
            if s.op == op and s.args == a and s.kwargs == kw and s.stage == stage:
                return sid
        nid = f"n{self._n}"; self._n += 1
        self.nodes[nid] = RNode(nid, parent, op, a, kw, stage=stage)
        if parent is not None:
            self.nodes[parent].children.append(nid)
        return nid

    def result(self, node: str, op: str, item: tuple[int, ...], value: Any) -> None:
        """Tag what a read gave back: an object (read further) or a collection's members (each an item)."""
        from ..web_core import WebCore
        from ...query.collection import Collection

        members = list(value) if isinstance(value, (list, tuple, Collection)) else None
        if op in _FANS and members is not None:
            each = self.child(node, "each")
            for k, m in enumerate(members):
                if isinstance(m, WebCore):
                    self.track(m, each, (*item, k))
        elif isinstance(value, WebCore):
            self.track(value, node, item)

    @property
    def empty(self) -> bool:
        return not self.nodes

    # -- compiling -------------------------------------------------------------------
    def compile(self, journey: Any) -> "tuple[Any, dict[str, str]]":
        """The journey + its reads as ONE plan (an ``Expr``), and where each read landed in it."""
        steps: dict[str, str] = {}
        base_len = len(journey._plan.steps)
        top = [n for n in self.nodes.values() if n.parent is None]
        # reads made at more than one point of the journey (before a click, after it): each extracted where it was
        # made -- resolve().extract(<before>).step(click).extract(<after>).project()
        if len({n.stage for n in top}) > 1:
            return self._staged(journey, top, steps)
        # one fan-out read on the page: the plan fans out at the top (rows); anything else: one row of columns
        if len(top) == 1 and top[0].op in _FANS and self._each(top[0].id):
            fan = top[0]
            chain = getattr(journey, fan.op)(*fan.args, **dict(fan.kwargs))
            steps[f"@{fan.id}"] = str(base_len)
            cols = self._columns(self._each(fan.id), f"{base_len + 2}", steps)
            plan = chain.extract(**cols).project() if cols else chain
        else:
            cols = self._columns(None, f"{base_len}", steps)
            plan = journey.extract(**cols) if cols else journey
        return plan, steps

    def _staged(self, journey: Any, top: "list[RNode]", steps: dict[str, str]) -> "tuple[Any, dict[str, str]]":
        """The journey with an ``extract`` of the reads made at each point of it, after the journey call they
        followed (a Document's extract stages its row and hands the page on); ``project`` renders the row."""
        from ...query.expr import Expr
        from ...query.plan import Arg, Step

        plan = journey._plan
        js = list(plan.steps)
        calls = [i for i, st in enumerate(js) if st.kind == "get"]  # each journey call starts at its get
        out: list[Step] = []
        taken: "dict[str, Any]" = {}  # the row accumulates across the extracts: a name used once is used
        for k, start in enumerate(calls):
            end = calls[k + 1] if k + 1 < len(calls) else len(js)
            out.extend(js[start:end])
            mine = [n.id for n in top if n.stage == k + 1]
            if not mine:
                continue
            cols = self._columns(None, str(len(out)), steps, tops=mine, taken=taken)
            taken.update(cols)
            if cols:
                out.append(Step(kind="get", name="extract"))
                out.append(Step(kind="call", name="extract", kwargs={name: Arg(plan=e._plan) for name, e in cols.items()}))
        out.extend([Step(kind="get", name="project"), Step(kind="call", name="project")])
        return Expr(plan.model_copy(update={"steps": out}), journey._client), steps

    def _each(self, fan: str) -> "str | None":
        return next((c for c in self.nodes[fan].children if self.nodes[c].op == "each"), None)

    def _columns(self, under: str | None, at: str, steps: dict[str, str], tops: "list[str] | None" = None, taken: "dict[str, Any] | None" = None) -> "dict[str, Any]":
        """The columns an extract at address ``at`` gets for the reads under ``under``: one per leaf read (a
        chain of ops from here), a nested fan-out as a column of rows."""
        from ...interface import wq

        cols: dict[str, Any] = {}
        kids = self.nodes[under].children if under is not None else (tops if tops is not None else [n.id for n in self.nodes.values() if n.parent is None])

        def walk(nid: str, expr: Any, path: list[RNode]) -> None:
            node = self.nodes[nid]
            expr = getattr(expr, node.op)(*node.args, **dict(node.kwargs))
            path = [*path, node]
            if node.op in _FANS and self._each(nid):
                name = self._name(path, {**(taken or {}), **cols}, rows=True)
                pos = len(path) - 1
                for k, p in enumerate(path):
                    steps.setdefault(f"@{p.id}", f"{at}/kw:{name}/{2 * k}")
                sub = self._columns(self._each(nid), f"{at}/kw:{name}/{2 * (pos + 1)}", steps)
                cols[name] = expr.extract(**sub).project() if sub else expr
                return
            reads = [c for c in node.children if self.nodes[c].op != "each"]
            if not reads:  # a leaf: the value this chain reads is a column
                name = self._name(path, {**(taken or {}), **cols})
                for k, p in enumerate(path):
                    steps.setdefault(f"@{p.id}", f"{at}/kw:{name}/{2 * k}")
                cols[name] = expr
                return
            for c in reads:
                walk(c, expr, path)

        for c in kids:
            if self.nodes[c].op != "each":
                walk(c, wq.doc, [])
        return cols

    @staticmethod
    def _name(path: list[RNode], taken: "dict[str, Any]", rows: bool = False) -> str:
        """A column name from what the chain reads: the last selector + the attribute (``h3_a_title``)."""
        sel = next((str(p.args[0]) for p in reversed(path) if p.op in ("select", "select_all", "links") and p.args), "")
        last = path[-1]
        what = "rows" if rows else str(last.args[0]) if last.op == "attr" and last.args else "text" if last.op == "text_content" else last.op
        base = re.sub(r"[^A-Za-z0-9]+", "_", f"{sel}_{what}" if sel else what).strip("_").lower() or "value"
        name, k = base, 2
        while name in taken:
            name, k = f"{base}_{k}", k + 1
        return name
