"""Compile a model-written ``wq`` chain into a runnable, self-contained query.

The onboard tier (an :class:`~web.onboard.llm.Llm`) replies with a ``wq.doc…`` chain, exactly as
the :mod:`patterns <web.onboard.patterns>` guide documents. This module turns that TEXT into a
:class:`~web.dsl.Expr` and roots it at the located URL, WITHOUT ``eval``:

* :func:`parse_query` walks the reply's AST and rebuilds the chain by driving the REAL ``wq``
  namespace -- attribute access + method calls on the recorder, the query operators (``& | ~`` and
  the comparisons a ``filter`` uses), and literal constants only. Any other name, a private
  (``_``) attribute, a starred/double-starred call, or a stray statement is refused, so a
  prompt-injected line like ``wq.reference.__globals__['os'].system(...)`` cannot execute -- it is
  rejected at the ``__globals__`` attribute, never run. This is the security boundary that lets a
  model author a query over an untrusted crawled page.
* :func:`reroot` prepends ``reference(url).resolve()`` (the guide has the model write a page-relative
  ``wq.doc`` chain; the pipeline supplies the source), composing the two recordings through the DSL's
  own public plan API so the result is one portable blob.
"""

from __future__ import annotations

import ast
import operator
from typing import cast

from web.dsl import Expr, LazyCollection, LazyDocument, Plan, from_plan, wq

#: any query Author can emit: rows / a scalar fan-out (a Collection) or a single document.
Query = LazyCollection[object] | LazyDocument

#: literal constant kinds a query may contain (selectors, nth indices, map values, flags).
_CONST = (str, int, float, bool, type(None))
#: typographic characters a model sometimes emits instead of the ASCII forms ``ast.parse`` needs.
_SMART = {
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
    "–": "-",
    "—": "-",
    "…": "...",
    " ": " ",
}
#: comparison operators allowed inside a ``filter`` predicate.
_CMP: dict[type[ast.cmpop], object] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}


class QueryError(ValueError):
    """The model's reply was not a rebuildable ``wq`` chain (unparseable, empty, or disallowed)."""


def query_code(reply: str) -> str:
    """The query EXPRESSION from a model reply: drop any code fence / prose, start at the first
    ``wq.`` (so a ``query =`` preamble goes), cut a trailing fence, and normalise smart quotes /
    dashes to the ASCII forms the parser needs -- so a stray typographic character does not fail.
    """
    text = reply.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
    start = text.find("wq.")
    if start != -1:
        text = text[start:]
    fence = text.find("```")
    if fence != -1:
        text = text[:fence]
    for bad, good in _SMART.items():
        text = text.replace(bad, good)
    return text.strip()


def _eval(node: ast.AST) -> object:
    """Interpret ONE query AST node against the real ``wq`` recorder. Only ``wq`` is a name;
    ``_``-prefixed attributes, starred args and any other construct are refused."""
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, _CONST):
            return node.value
        raise QueryError(f"disallowed constant: {node.value!r}")
    if isinstance(node, ast.Name):
        if node.id == "wq":
            return wq
        raise QueryError(f"only 'wq' is available in a query, not {node.id!r}")
    if isinstance(node, ast.Attribute):
        if node.attr.startswith("_"):
            raise QueryError(f"attribute {node.attr!r} is not allowed in a query")
        return getattr(_eval(node.value), node.attr)
    if isinstance(node, ast.Call):
        if any(isinstance(a, ast.Starred) for a in node.args):
            raise QueryError("*args are not allowed in a query")
        func = _eval(node.func)
        args = [_eval(a) for a in node.args]
        kwargs: dict[str, object] = {}
        for kw in node.keywords:
            if kw.arg is None:
                raise QueryError("**kwargs are not allowed in a query")
            kwargs[kw.arg] = _eval(kw.value)
        return cast("object", func(*args, **kwargs))  # type: ignore[operator]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Invert):  # ~cond
        return ~_eval(node.operand)  # type: ignore[operator]
    if isinstance(node, ast.BinOp) and isinstance(
        node.op, (ast.BitAnd, ast.BitOr)
    ):  # a & b / a | b
        left, right = _eval(node.left), _eval(node.right)
        return left & right if isinstance(node.op, ast.BitAnd) else left | right  # type: ignore[operator]
    if isinstance(node, ast.Compare) and len(node.ops) == 1:  # a == b, a < b, ...
        fn = _CMP.get(type(node.ops[0]))
        if fn is None:
            raise QueryError("that comparison is not allowed in a query")
        return cast("object", fn(_eval(node.left), _eval(node.comparators[0])))  # type: ignore[operator]
    raise QueryError(f"disallowed expression in a query: {type(node).__name__}")


def parse_query(reply: str) -> Expr:
    """Rebuild the model's ``wq`` chain from its reply through the real recorder (never ``eval``;
    see the module docstring). Raises :class:`QueryError` on an empty / unparseable / disallowed
    reply, or one that does not evaluate to a recorded chain."""
    code = query_code(reply)
    if not code:
        raise QueryError("no query in the reply")
    try:
        tree = ast.parse(code, mode="eval")
    except SyntaxError as exc:
        raise QueryError(f"query did not parse: {exc}") from exc
    result = _eval(tree)
    if not isinstance(result, Expr):
        raise QueryError(f"query is a {type(result).__name__}, not a wq chain")
    return result


def reroot(chain: Expr, url: str, *, profile: "str | None" = None) -> Query:
    """Root a page-relative ``wq.doc`` chain at ``url`` by prepending ``reference(url).resolve()``
    -- composed through the DSL's public plan API so the result is one self-contained, portable
    blob. ``profile`` bakes the KNOWN-good transport into the root (``resolve(profile=...)``) -- the
    profile Locate found works, so the query uses it instead of re-running resolve's escalation
    discovery. A chain the model already rooted at a ``reference(...)`` (it has a source) is left.
    """
    tail = Plan.from_blob(chain.to_blob())
    if tail.source is not None:  # already self-contained -- don't double-root
        return cast(Query, chain)
    root = wq.reference(url).resolve(profile=profile) if profile else wq.reference(url).resolve()
    base = Plan.from_blob(cast(Expr, root).to_blob())
    merged = Plan(root=base.root, source=base.source, steps=[*base.steps, *tail.steps])
    return cast(Query, from_plan(merged))


__all__ = ["Query", "QueryError", "parse_query", "query_code", "reroot"]
