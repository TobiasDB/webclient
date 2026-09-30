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

from collections.abc import Sequence
from typing import cast

from web.dsl import (
    Arg,
    Expr,
    LazyCollection,
    LazyDocument,
    Plan,
    SourceError,
    Step,
    UnknownVerb,
    from_plan,
    from_source,
    wq,
)

#: any query Author can emit: rows / a scalar fan-out (a Collection) or a single document.
Query = LazyCollection[object] | LazyDocument

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


class QueryError(ValueError):
    """The model's reply was not a rebuildable ``wq`` chain (unparseable, empty, or disallowed).
    ``verbs`` are the DSL verbs the reply reached for anyway and ``unknown`` the ones the DSL does
    not have -- kept so a rejected reply still counts toward the verb record (the gap list)."""

    def __init__(
        self, message: str, *, verbs: "Sequence[str]" = (), unknown: "Sequence[str]" = ()
    ) -> None:
        super().__init__(message)
        self.verbs = list(verbs)
        self.unknown = list(unknown)


def clean_reply(reply: str) -> str:
    """A model reply as PARSEABLE code: drop a leading code fence (and its language tag), cut at a
    trailing fence, and normalise smart quotes / dashes to the ASCII forms ``ast`` needs -- so a
    stray typographic character never fails a parse. Shared by the whole-chain and the step parsers.
    """
    text = reply.strip()
    if "```" in text:  # take the FENCED body (prose may precede the fence); else drop stray fences
        rest = text[text.find("```") :].split("\n", 1)
        body = rest[1] if len(rest) > 1 else ""
        close = body.find("```")
        inner = body[:close] if close != -1 else body
        text = inner if inner.strip() else text.replace("```", "")
    for bad, good in _SMART.items():
        text = text.replace(bad, good)
    return text.strip()


def query_code(reply: str) -> str:
    """The query EXPRESSION from a model reply: :func:`clean_reply`, then start at the first
    ``wq.`` (so a ``query =`` preamble / prose goes)."""
    text = clean_reply(reply)
    start = text.find("wq.")
    if start != -1:
        text = text[start:]
    return text.strip()


def parse_query(reply: str) -> Expr:
    """Rebuild the model's ``wq`` chain from its reply. Cleans the reply (fences / smart quotes /
    prose -- the model tier's concern) with :func:`query_code`, then hands the clean source to the
    DSL's safe functional parser :func:`web.dsl.from_source` (never ``eval``; only ``wq`` is in
    scope). Raises :class:`QueryError` on an empty / unparseable / disallowed reply."""
    code = query_code(reply)
    if not code:
        raise QueryError("no query in the reply")
    try:
        return from_source(code)
    except UnknownVerb as exc:  # a verb the DSL lacks -> the reason names the gap
        raise QueryError(str(exc), verbs=exc.used, unknown=exc.verbs) from exc
    except SourceError as exc:  # the DSL's parse error -> the onboard tier's QueryError
        raise QueryError(str(exc)) from exc


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


def keyed(query: Query, fields: "Sequence[str]", *, document: str = "") -> Query:
    """``query`` with the IDENTITY step appended -- ``.key(*fields, document=css)`` -- the way the
    pipeline declares what makes a row unique (from the brief), never the model. A query that
    already ends in a ``key`` step is left as it is."""
    plan = Plan.from_blob(query.to_blob())
    if any(s.kind == "get" and s.name == "key" for s in plan.steps):
        return query
    steps = list(plan.steps)
    # a trailing `.project()` (the guide lets the model write it) materialises a plain list -- the
    # identity step must come BEFORE it; the terminals project implicitly, so it is simply dropped.
    while len(steps) >= 2 and steps[-2].kind == "get" and steps[-2].name == "project":
        steps = steps[:-2]
    plan = plan.model_copy(update={"steps": steps})
    args = [Arg(value=f) for f in fields]
    kwargs = {"document": Arg(value=document)} if document else {}
    step_get = Step(kind="get", name="key")
    step_call = Step(kind="call", args=args, kwargs=kwargs)
    return cast(Query, from_plan(plan.extend(step_get).extend(step_call)))


__all__ = ["Query", "QueryError", "clean_reply", "keyed", "parse_query", "query_code", "reroot"]
