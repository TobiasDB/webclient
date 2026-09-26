"""onboarding.query -- see the package docstring."""


import abc
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Sequence

from pydantic import BaseModel, model_validator

#: the pipeline's logger. Stages log progress here (seeds, crawl, candidates, the
#: evaluation, the query, spend); a CLI or app sets the level / handler. Each line is
#: also appended to ``OnboardingResult.steps`` for a programmatic trace.

from ...core.crawl import from_picks
from ...core.document.models import Flag, PaginationHint
from ...policy import (
    AntiBotPolicy,
    BrowserPolicy,
    ProxyPolicy,
    Resolve,
)
from ...llm.guides import lazy_query_guide
from ...query.expr import from_blob
from ...interface import Reference, WebClient, wq
from ...clients.llm import Budget, BudgetExceeded, LlmClient, LlmError
from ...llm.prompts import render_prompt

from .dates import _dig, _timeliness
from .common import LLM, BrowserMode, _skeleton_for, log
from .artifacts import Brief, CandidateEval, QueryPart, QueryArtifact
from .llm import _strip_fences, _json_blob, _ask_json, _fields_line


# --------------------------------------------------------------------------- #
# 7. write_query  (LLM authors a lazy query from the skeleton)
# --------------------------------------------------------------------------- #


def _query_prompt(brief: Brief, skeleton: str, *, paginated: bool = False, recency: str = "") -> str:
    # Deliberately narrow: the packaged query spec + the skeleton + the ask. Nothing
    # about fetching, resolving, or running -- only CSS selectors and the query syntax.
    pager = (
        "\nThe dataset spans multiple pages -- write the query for ONE page exactly as normal; "
        "the pipeline follows the pagination automatically. Do NOT add a 'next' field."
        if paginated else ""
    )
    return render_prompt(
        "write_query",
        guide=lazy_query_guide(),
        description=brief.description,
        fields_line=_fields_line(brief),
        pager=pager,
        skeleton=skeleton,
        hints=(f"\n\nDATASET NOTES (from the brief -- how this dataset is laid out): {brief.hints}"
               if brief.hints else ""),
        recency=(f"\n\nRECENCY (from the page evaluation): {recency}" if recency else ""),
    )


def _recency_guidance(ev: "CandidateEval | None") -> str:
    """A short recency instruction for the query writer, from the evaluator's read of the
    sort order + where the most recent records are. Empty for a non-dated dataset."""
    if ev is None:
        return ""
    parts = []
    if ev.sort_order:
        parts.append(f"the records are {ev.sort_order}")
    if ev.recency_hint:
        parts.append(ev.recency_hint)
    if not parts:
        return ""
    return "Prioritise the MOST RECENT records -- " + "; ".join(parts) + "."


def _data_rows(result: Any) -> list[Any]:
    """The EXTRACTED DATA a query produced -- plain rows (dicts / scalars), never the
    elements themselves. A query that stops at ``select_all`` yields a collection of
    ``Document`` elements: that is a selection, NOT extracted data, so it counts as
    zero rows (the author must ``.project()``). This is what stops an un-projected
    query from looking like a success and what keeps the sample as data, not objects."""
    from ...core.web_core import WebCore

    if result is None or isinstance(result, WebCore):
        return []  # None, or a single selected element -- not data
    try:
        items = list(result)
    except TypeError:  # a scalar / Field -- one value
        return [result]
    if items and all(isinstance(i, WebCore) for i in items):
        return []  # a collection of elements -- selected, not extracted (no project)
    return items


def _extraction_steps(doc_expr: Any) -> list[Any]:
    """The model's EXTRACTION steps only -- from the first ``select``/``select_all``
    onward -- dropping any navigation (a stray ``resolve``) it may have prefixed. So the
    join with the reference + resolve is DETERMINISTIC: the model supplies the selection,
    the pipeline supplies exactly one reference + one resolve."""
    steps = list(doc_expr._plan.steps)
    for i, s in enumerate(steps):
        if s.kind == "get" and s.name in ("select", "select_all"):
            return steps[i:]
    return steps


def _paginate_steps(max_pages: int = 50, hint: "PaginationHint | None" = None) -> list[Any]:
    """The plan steps for the ``.paginate(...)`` the detected :class:`PaginationHint` suggests (its
    best mode: next link / page param / load-more), spliced between the reference resolve and the
    extraction so the shipped query walks the dataset's pages and the body extracts across all of
    them. No hint: follow ``rel=next`` / the HTTP Link header. Authoring still tests page one only."""
    from ...core.document.paginate import pager_kwargs

    plan = wq.doc.paginate(**pager_kwargs(hint.best if hint is not None else None), max_pages=max_pages)
    return list(plan._plan.steps)


def _executable_query(
    doc_expr: Any, url: str, resolve: "Resolve | None", *, paginate: bool = False, max_pages: int = 50,
    hint: "PaginationHint | None" = None,
) -> Any:
    """DETERMINISTICALLY wrap the model's DOCUMENT-level extraction into a SELF-CONTAINED
    query rooted at the source reference with a ``resolve`` step baked in, so
    ``from_blob(blob).collect()`` fetches + resolves + extracts with no context --
    executable exactly as output. The model supplies only the extraction; this function
    (no LLM) supplies the reference + resolve. When the source needs proxy / antibot, the
    FULL policy is baked in (``resolve(policy=...)``) so the blob re-fetches with it; a
    plain source just bakes the browser tier. ``paginate`` splices the hinted ``.paginate(...)``
    after the resolve, so a paginated source's blob pulls the WHOLE dataset (not page one)."""
    from ...query.expr import Expr
    from ...query.plan import Plan

    ref = wq.reference(url)
    if resolve is not None and (resolve.proxy is not None or resolve.antibot is not None):
        rooted = ref.resolve(policy=resolve.model_dump(mode="json"))  # full policy in the blob
    else:
        tier = resolve.browser.when if (resolve is not None and resolve.browser is not None) else None
        rooted = ref.resolve(browser=tier) if tier else ref.resolve()
    pag = _paginate_steps(max_pages, hint) if paginate else []
    steps = [*rooted._plan.steps, *pag, *_extraction_steps(doc_expr)]
    return Expr(Plan(root="Reference", source=rooted._plan.source, steps=steps), doc_expr._client)


def _reroot(expr: Any, url: str) -> Any:
    """A copy of a reference-rooted executable query re-pointed at ``url`` (so ONE
    authored query runs against each of several base URLs)."""
    from ...core.reference import from_url
    from ...query.expr import Expr
    from ...query.plan import Plan

    return Expr(
        Plan(root="Reference", source=from_url(url).model_dump(), steps=expr._plan.steps),
        expr._client,
    )


def _query_code(reply: str) -> str:
    """The query EXPRESSION from the model's reply: strip any code fence / prose and start
    at the first ``wq.`` so a leading ``query =`` assignment or preamble is dropped, and cut
    a trailing code fence (a model that wraps the code in ``` despite the ask)."""
    t = _strip_fences(reply)
    i = t.find("wq.")
    if i != -1:
        t = t[i:]
    fence = t.find("```")  # a trailing fence when prose preceded the opening one
    if fence != -1:
        t = t[:fence]
    return t.strip()


#: constant literals a query may contain (selectors, group indices, flags).
_QUERY_CONST = (str, int, float, bool, bytes, type(None))


def _eval_query_ast(node: Any, root: Any) -> Any:
    """Interpret ONE node of a written query, driving the REAL ``wq`` interface -- attribute
    access and method calls on our own Expr / backing ops only. This is NOT ``eval``: the
    only name is ``wq``, attributes starting with ``_`` are refused (so ``__globals__`` /
    ``__class__`` and the builtins they reach are unreachable), only literal constants and
    the query operators (``& | ~`` and the comparisons used in ``filter``) are allowed, and
    anything else raises. So a prompt-injected line like
    ``wq.reference.__globals__['os'].system(...)`` cannot execute -- it is rejected at the
    ``__globals__`` attribute, never run."""
    import ast

    if isinstance(node, ast.Expression):
        return _eval_query_ast(node.body, root)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, _QUERY_CONST):
            return node.value
        raise ValueError(f"disallowed constant: {node.value!r}")
    if isinstance(node, ast.Name):
        if node.id == "wq":
            return root
        raise ValueError(f"only 'wq' is available in a query, not {node.id!r}")
    if isinstance(node, ast.Attribute):
        if node.attr.startswith("_"):
            raise ValueError(f"attribute {node.attr!r} is not allowed in a query")
        return getattr(_eval_query_ast(node.value, root), node.attr)
    if isinstance(node, ast.Call):
        func = _eval_query_ast(node.func, root)
        if any(isinstance(a, ast.Starred) for a in node.args):
            raise ValueError("*args are not allowed in a query")
        args = [_eval_query_ast(a, root) for a in node.args]
        kwargs: dict[str, Any] = {}
        for kw in node.keywords:
            if kw.arg is None:
                raise ValueError("**kwargs are not allowed in a query")
            kwargs[kw.arg] = _eval_query_ast(kw.value, root)
        return func(*args, **kwargs)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Invert):  # ~cond in a filter
        return ~_eval_query_ast(node.operand, root)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.BitAnd, ast.BitOr)):  # a & b / a | b
        left, right = _eval_query_ast(node.left, root), _eval_query_ast(node.right, root)
        return (left & right) if isinstance(node.op, ast.BitAnd) else (left | right)
    if isinstance(node, ast.Compare) and len(node.ops) == 1:  # a == b, a < b, ...
        import operator as _op
        ops = {ast.Eq: _op.eq, ast.NotEq: _op.ne, ast.Lt: _op.lt,
               ast.LtE: _op.le, ast.Gt: _op.gt, ast.GtE: _op.ge}
        fn = ops.get(type(node.ops[0]))
        if fn is None:
            raise ValueError("that comparison is not allowed in a query")
        return fn(_eval_query_ast(node.left, root), _eval_query_ast(node.comparators[0], root))
    raise ValueError(f"disallowed expression in a query: {type(node).__name__}")


def _parse_query(reply: str) -> Any:
    """Load the model's query. The model WRITES it as a ``wq.doc`` chain -- exactly as the
    guide documents -- and we rebuild it THROUGH OUR OWN INTERFACE: the code is parsed to an
    AST and interpreted by :func:`_eval_query_ast`, which drives only the real ``wq`` Expr /
    backing ops (attribute access + method calls with literal args, plus the query operators).
    It is NOT ``eval`` -- a prompt-injected line reaching ``__globals__`` or any non-``wq``
    name is refused before anything runs, so a hostile crawled page cannot achieve code
    execution. A raw ``to_blob()`` blob is still accepted as a fallback."""
    from ...query.expr import Expr

    import ast

    code = _query_code(reply)
    if code.startswith("wq."):
        # rebuild THROUGH OUR INTERFACE via a controlled AST walk -- NOT eval(): a
        # prompt-injected line reaching __globals__ or a non-wq name is refused first.
        expr = _eval_query_ast(ast.parse(code, mode="eval"), wq)
        if not isinstance(expr, Expr):
            raise TypeError(f"query is a {type(expr).__name__}, not a wq.doc chain")
        return _normalize_selectors(expr)  # CSS '>' child combinator -> safer descendant space
    return _normalize_selectors(from_blob(_json_blob(reply)))  # fallback: a raw blob


#: a delimiter line separating the per-section queries of a SPLIT dataset -- a line that is
#: only dashes (``---`` or longer), which cannot occur inside a single ``wq.`` chain.
_QUERY_SPLIT = __import__("re").compile(r"^\s*-{3,}\s*$", __import__("re").M)


def _split_queries(reply: str) -> "list[str]":
    """Split a model reply into its SECTION queries. A split dataset is written as one simple
    ``wq.doc...project()`` per section separated by a line of only dashes (``---``); the common
    single-section reply has no delimiter and yields one segment. Fences are stripped first
    (reuse :func:`_strip_fences`) and only segments that actually contain a ``wq.`` chain are
    kept, so a stray delimiter or blank tail doesn't create an empty query."""
    text = _strip_fences(reply)
    segments = [s.strip() for s in _QUERY_SPLIT.split(text)]
    kept = [s for s in segments if "wq." in s]
    return kept or [text]  # no wq. anywhere -> hand the whole reply on (raw-blob fallback)


def _parse_queries(reply: str) -> "list[Any]":
    """Parse a reply into 1..N section queries (:func:`_parse_query` per segment). One segment
    -> one query (today's path, incl. the raw-blob fallback); a ``---``-separated reply ->
    several. Each segment goes through the SAME safe AST allowlist, so multi-section parsing
    adds no new execution surface. Raises if a segment is not a valid query."""
    return [_parse_query(seg) for seg in _split_queries(reply)]


def _row_selector(expr: Any) -> "str | None":
    """The CSS/selector string the query selects its repeating record with -- the argument
    of the first ``select``/``select_all``. Used to diagnose a 0-row query: if this
    selector matches nothing on the page, the row selector itself is wrong."""
    steps = list(expr._plan.steps)
    for i, s in enumerate(steps):
        if s.kind == "get" and s.name in ("select", "select_all"):
            nxt = steps[i + 1] if i + 1 < len(steps) else None
            if nxt is not None and nxt.kind == "call" and nxt.args:
                return getattr(nxt.args[0], "value", None)
    return None


def _selector_match_count(sel: "str | None", doc: Any) -> "int | None":
    """How many elements ``sel`` matches on the fetched ``doc`` (``None`` if it can't be
    probed). Lets the feedback tell the model whether its ROW selector is wrong (0 matches)
    or whether the record matches but the FIELD extraction is (matches, but no data)."""
    if not sel or not doc.ok:
        return None
    try:
        probe = wq.doc.select_all(sel).extract(_=wq.doc.attr("text")).project()
        return len(_data_rows(probe.collect(doc)))
    except Exception:  # noqa: BLE001 - a selector the engine can't run -> unknown
        return None


def _no_rows_hint(expr: Any, doc: Any) -> str:
    """A human-readable, actionable hint for why a query extracted 0 rows: either the ROW
    selector matched nothing (wrong record selector) or it matched records but no fields
    came out (wrong field selectors / missing ``.project()``). Guides the retry."""
    sel = _row_selector(expr)
    n = _selector_match_count(sel, doc)
    ops = {s.name for s in expr._plan.steps if s.kind == "get"}
    projected = "project" in ops  # did the query actually extract+project fields?
    if n == 0:
        return (
            f'Your record selector "{sel}" matched NO elements on this page, so nothing was'
            " extracted. Look again at the skeleton and pick a selector that matches ONE"
            " element per record (a repeated tag/class you can see in the skeleton)."
        )
    if n and not projected:
        # the record selector matched, but the query never extracted -- the classic "selected
        # elements, forgot to pull fields" -- so it produced 0 DATA rows.
        return (
            f'Your record selector "{sel}" matched {n} record(s), but your query only SELECTED'
            " them -- it never extracted fields, so it produced 0 data rows. Add"
            " .extract(col=wq.doc.select(...).attr(...), ...) for each field and END with"
            " .project()."
        )
    if n:
        return (
            f'Your record selector "{sel}" matched {n} element(s), but NONE of your field'
            " selectors found a value inside them. Two likely causes: (1) the record selector is"
            " too broad -- it matched a WRAPPER, not one record each. Prefer a SEMANTIC anchor: a"
            ' repeated <article>/<li>/<tr>, or [class*="product"]/[class*="post"] describing the'
            ' record -- NOT a hashed build class (e.g. ".AMTIxG_grid", ".css-1a2b3c"), which names'
            " a styling box, not a record. (2) your field selectors are right relative to the"
            " record but match nothing in it. Use the record HTML below: pick the element that"
            " wraps exactly ONE record, then field selectors you can SEE inside it."
        )
    return (
        "Your query ran but extracted 0 data rows: it MUST .select_all(<record selector>),"
        " pull each field with .extract(col=...), and END with .project() so it returns"
        " data rows -- not selected elements. Re-check your selectors against the skeleton."
    )


def _sample_record_html(expr: Any, doc: Any, *, limit: int = 900) -> str:
    """The FIRST matched record's own markup (whitespace-collapsed, trimmed) -- so a retry
    hint can SHOW the model the exact element it must extract from. This is what turns a
    vague "a field is empty" into a fixable one: the record's HTML reveals values that live
    in an ATTRIBUTE (``data-rating="Four"``, ``datetime=...``) rather than in text, so the
    model can switch ``.attr("text")`` to ``.attr("data-rating")``. Empty if unprobeable."""
    sel = _row_selector(expr)
    if not sel or not doc.ok:
        return ""
    try:
        first = wq.doc.select(sel).collect(doc)  # the first matching record element
        html = first.html() if getattr(first, "ok", False) else ""
    except Exception:  # noqa: BLE001 - a selector the engine can't run -> no sample
        return ""
    html = " ".join(html.split())
    return html[:limit] + ("…" if len(html) > limit else "")


def _nonempty(v: Any) -> bool:
    """Whether an extracted value actually carries content -- not ``None``, not blank/
    whitespace, not an empty list/dict. The test of "did the selector match content"."""
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip() != ""
    if isinstance(v, (list, dict, tuple, set)):
        return len(v) > 0
    return True


def _populated_rows(rows: "list[Any]") -> "list[Any]":
    """The rows that carry AT LEAST ONE non-empty field. A row of all-empty cells means
    the record selector matched an element but every FIELD selector matched nothing (a
    guessed query, or content that isn't in this HTML) -- it is not real extracted data,
    so it must not count as a extracted row."""
    out: list[Any] = []
    for r in rows:
        if isinstance(r, dict):
            if any(_nonempty(v) for v in r.values()):
                out.append(r)
        elif _nonempty(r):
            out.append(r)
    return out


def _required_leaf_paths(brief: Brief) -> "list[str]":
    """The required LEAF field paths (dotted). A leaf is a field with no deeper field under
    it (``price.value`` / ``price.unit`` are leaves; ``price`` is their branch). A leaf is
    optional if IT or any ancestor is marked optional. Used to validate nested extractions:
    a query that produced ``price = {"value":"","unit":""}`` populated the branch but NONE of
    its required leaves, which the old top-level check missed."""
    fields = list(brief.fields)
    opt = set(brief.optional)
    leaves = [f for f in fields if not any(g != f and g.startswith(f + ".") for g in fields)]
    req: list[str] = []
    for leaf in leaves:
        parts = leaf.split(".")
        ancestors = [".".join(parts[: i + 1]) for i in range(len(parts))]
        if not any(a in opt for a in ancestors):  # leaf or an ancestor optional -> skip
            req.append(leaf)
    return req


def _empty_required_fields(rows: "list[Any]", brief: Brief) -> "list[str]":
    """Required LEAF paths that are EMPTY (or absent) across every row -- their selectors
    matched no content, so the query is only a partial guess. Recurses into nested rows via
    :func:`_dig`, so an empty nested leaf (``price.value``) is caught even when its branch dict
    is present. Empty when the rows carry every required leaf; skipped with no schema."""
    req = _required_leaf_paths(brief)
    dict_rows = [r for r in rows if isinstance(r, dict)]
    if not req or not dict_rows:
        return []
    return [p for p in req if not any(_nonempty(_dig(r, p)) for r in dict_rows)]


def _content_hint(expr: Any, rows: "list[Any]", brief: Brief, doc: Any) -> str:
    """The retry hint when a query RAN but did not truly extract the dataset -- naming the
    specific validation that failed (record selector matched nothing / matched but fields
    are empty / a required field is empty) and warning that the content may not be in the
    HTML at all (client-rendered / iframe / shadow DOM), which a static query can't reach."""
    caveat = (
        " If the records are not visible in the skeleton at all, the page is likely rendered"
        " client-side (an SPA) or the data sits inside an iframe or shadow DOM -- a static"
        " query cannot reach it; do NOT guess selectors that are not in the skeleton."
    )
    sample = _sample_record_html(expr, doc)
    shown = (
        f"\n\nHere is the FIRST matched record's HTML -- find the missing field(s) IN IT. A "
        f"value may live in an ATTRIBUTE (e.g. data-rating=\"Four\", datetime=\"...\") rather "
        f"than in the element text: read it with .attr(\"<name>\"), not .attr(\"text\"). Do not "
        f"add fields that are genuinely absent here.\n{sample}"
        if sample else ""
    )
    if not _populated_rows(rows):  # matched a container but every field is empty (or 0 rows)
        return _no_rows_hint(expr, doc) + shown + caveat
    empty = _empty_required_fields(rows, brief)  # some required field never came out
    cols = ", ".join(f'"{c}"' for c in empty)
    return (
        f"Your query extracted rows, but the required field(s) {cols} were EMPTY on every"
        " row. Either their selector matched no element, OR it matched an element whose TEXT"
        " is empty because the value is in an attribute (use .attr(\"<name>\"))." + shown + caveat
    )


#: hard wall-clock cap on running ONE authored query against the source. A pathological LLM
#: query (a per-record .resolve() fanning out to hundreds of fetches, a resolve to a slow host)
#: must never hang the pipeline: the test is cancelled at this bound and counts as a failure.
_QUERY_TEST_TIMEOUT = 45.0


def _test_query(expr: Any, doc: Any, *, timeout: float = _QUERY_TEST_TIMEOUT) -> "tuple[bool, list[Any]]":
    """Run the authored query against the fetched source ``doc`` to prove it loads and
    actually EXTRACTS the dataset. The query is the document-level extraction
    (``wq.doc...``), so it collects directly against the resolved document. Returns
    ``(ran_without_error, rows)`` -- a query that only selects elements (no ``.project()``)
    extracts zero rows and so is not treated as a working query.

    HARD-BOUNDED: the run is cancelled after ``timeout`` seconds (``asyncio.wait_for`` cancels
    the coroutine, so in-flight fetches stop), so no authored query can hang the pipeline."""
    import asyncio

    loop = getattr(getattr(doc, "_client", None), "loop", None)
    try:
        if callable(loop):  # bound + cancel on the engine loop (the normal path)
            async def _bounded() -> Any:
                return await asyncio.wait_for(expr.acollect(doc), timeout=timeout)

            engine: Any = loop()
            result = engine.run(_bounded())
        else:  # no engine loop (an unbound doc) -- fall back to the plain sync collect
            result = expr.collect(doc)
    except (asyncio.TimeoutError, TimeoutError):
        log.warning("    query test exceeded %.0fs and was cancelled -- treated as a FAILED "
                    "attempt (a query must not hang; avoid per-record .resolve() over many rows)",
                    timeout)
        return False, []
    except Exception:  # noqa: BLE001 - a query that can't run against the source
        return False, []
    return True, _data_rows(result)


def run_query(artifact: QueryArtifact, *, wc: WebClient) -> list[Any]:
    """Run the authored query and concatenate all the rows. Two independent join axes both
    concatenate: every SECTION sub-query (``artifact.parts`` -- an UPCOMING callout + an
    ARCHIVED list on one page) and every base URL (``base_urls`` -- ``/products/cloud`` +
    ``/products/onprem``). A plain single-section query has no ``parts`` and runs via ``blob``.
    Each blob is SELF-CONTAINED (reference + resolve + extraction), so it resolves + extracts
    on its own; each base just re-points the reference."""
    blobs = [p.blob for p in artifact.parts] or [artifact.blob]
    out: list[Any] = []
    for blob in blobs:  # each section sub-query
        base_expr = from_blob(blob, wc)
        for url in artifact.base_urls or []:
            try:
                result = _reroot(base_expr, url).collect()  # self-contained: no context
            except Exception:  # noqa: BLE001 - a base whose query fails contributes nothing
                continue
            out.extend(_data_rows(result))  # extracted data rows, not selected elements
    return out


#: class tokens in a selector: ``.foo`` / ``tag.foo`` / ``[class*="foo"]`` / ``[class~=foo]``.
_SEL_CLASS = __import__("re").compile(r'\.([A-Za-z_][\w-]*)|\[class[*~^$|]?=["\']?([A-Za-z_][\w-]*)')


def _record_classes(record: Any) -> "set[str]":
    """Every class token present in a record's subtree -- the real hooks a field selector could
    use, so a near-miss selector (``.widget`` for a real ``widgets``) can be repaired to one."""
    out: set[str] = set()
    try:
        el = record._element
        nodes = [el, *el.iter()] if el is not None else []
    except Exception:  # noqa: BLE001
        return out
    for node in nodes:
        cls = node.get("class") if hasattr(node, "get") else None
        if isinstance(cls, str):
            out.update(cls.split())
    return out


def _selector_hits(record: Any, selector: str) -> bool:
    """Whether ``selector`` matches at least one element inside the record (relative to it)."""
    return (_selector_match_count(selector, record) or 0) > 0


def _repair_selector(selector: str, classes: "set[str]") -> "str | None":
    """Repair a class-based selector whose class token isn't present, by swapping in the NEAREST
    real class in the record -- fixing a plural/typo/mis-transcribed high-entropy class
    (``widget``->``widgets``, ``prodcut``->``product``). Returns the repaired selector, or None
    if no token needs (or has) a close-enough real match."""
    import difflib
    import re as _re

    new = selector
    for m in _SEL_CLASS.finditer(selector):
        tok = m.group(1) or m.group(2)
        if not tok or tok in classes:
            continue  # this class token already exists -- leave it
        best, cand = 0.0, None
        for c in classes:
            r = difflib.SequenceMatcher(None, tok, c).ratio()
            # a genuine plural / one-off typo: one is a prefix of the other, BOTH are non-trivial,
            # and the lengths are close -- NOT a tiny class that happens to prefix a longer word
            # (".nodate" must NOT snap to a real ".n").
            if ((c.startswith(tok) or tok.startswith(c))
                    and min(len(tok), len(c)) >= 3 and abs(len(tok) - len(c)) <= 3):
                r = max(r, 0.9)
            if r > best:
                best, cand = r, c
        # a bit relaxed: the repaired query + its data are still validated and reviewed downstream,
        # which catches a wrong swap -- so we can afford to try a slightly looser near-match.
        if cand and best >= 0.75:  # swap the mistyped token for the real class
            new = _re.sub(rf"(?<![\w-]){_re.escape(tok)}(?![\w-])", cand, new)
    return new if new != selector else None


def _iter_field_select_args(plan: "dict[str, Any]") -> "list[dict[str, Any]]":
    """Every field-selector arg node (``{"value": "<selector>"}``) inside the extract sub-plans of
    a query plan (recursing into nested sub-extracts). NOT the top-level record selector -- only
    the FIELD selectors, which is what a repair targets. Each returned dict can be mutated in place."""
    out: list[dict[str, Any]] = []

    def walk(p: "dict[str, Any]", *, in_field: bool) -> None:
        steps = p.get("steps", [])
        for i, s in enumerate(steps):
            if in_field and s.get("kind") == "get" and s.get("name") in ("select", "select_all"):
                nxt = steps[i + 1] if i + 1 < len(steps) else None
                if nxt and nxt.get("kind") == "call" and nxt.get("args"):
                    arg = nxt["args"][0]
                    if isinstance(arg, dict) and isinstance(arg.get("value"), str):
                        out.append(arg)
            if s.get("kind") == "call":  # descend into field sub-plans (extract kwargs / args)
                for v in list(s.get("kwargs", {}).values()) + list(s.get("args", [])):
                    if isinstance(v, dict) and isinstance(v.get("plan"), dict):
                        walk(v["plan"], in_field=True)

    walk(plan, in_field=False)
    return out


def _iter_all_select_args(plan: "dict[str, Any]") -> "list[dict[str, Any]]":
    """Every ``select``/``select_all`` selector arg node in a plan -- the record selector AND all
    field selectors (recursing into sub-extracts). Each dict can be mutated in place."""
    out: list[dict[str, Any]] = []

    def walk(p: "dict[str, Any]") -> None:
        steps = p.get("steps", [])
        for i, s in enumerate(steps):
            if s.get("kind") == "get" and s.get("name") in ("select", "select_all"):
                nxt = steps[i + 1] if i + 1 < len(steps) else None
                if nxt and nxt.get("kind") == "call" and nxt.get("args"):
                    arg = nxt["args"][0]
                    if isinstance(arg, dict) and isinstance(arg.get("value"), str):
                        out.append(arg)
            if s.get("kind") == "call":
                for v in list(s.get("kwargs", {}).values()) + list(s.get("args", [])):
                    if isinstance(v, dict) and isinstance(v.get("plan"), dict):
                        walk(v["plan"])

    walk(plan)
    return out


#: the strict CSS child combinator, with any surrounding whitespace.
_CHILD_COMBINATOR = __import__("re").compile(r"\s*>\s*")


def _normalize_selectors(expr: Any) -> Any:
    """Make a loaded query's CSS selectors more robust: replace the strict child combinator ``>``
    with a descendant space -- a direct-child selector (``ul > li``) breaks the moment a wrapper
    is inserted, whereas the descendant form (``ul li``) still matches, and for extraction the
    two almost always mean the same set. XPath selectors (starting with ``/`` // ``.//``) are left
    untouched. Returns the same expr if nothing changed, else a rebuilt one."""
    import copy as _copy

    plan = _copy.deepcopy(expr._plan.model_dump(mode="json"))
    changed = False
    for arg in _iter_all_select_args(plan):
        sel = arg["value"]
        if isinstance(sel, str) and ">" in sel and not sel.lstrip().startswith(("/", ".//")):
            fixed = _CHILD_COMBINATOR.sub(" ", sel).strip()
            if fixed != sel:
                arg["value"] = fixed
                changed = True
    if not changed:
        return expr
    from ...query.expr import Expr
    from ...query.plan import Plan

    return Expr(Plan.model_validate(plan), expr._client)


def _repair_query(expr: Any, doc: Any) -> Any:
    """Repair a query whose FIELD selectors have near-miss class typos: for each field selector
    that matches nothing inside a matched record, swap the mistyped class for the nearest real one
    present in the record (see :func:`_repair_selector`), rebuild the query, and hand it back for
    re-validation. Returns a repaired Expr, or None if nothing safe to repair. Deterministic and
    conservative -- only high-confidence class swaps that then actually match are applied."""
    import copy as _copy

    row_sel = _row_selector(expr)
    if not row_sel or not getattr(doc, "ok", False):
        return None
    try:
        record = wq.doc.select(row_sel).collect(doc)  # the first matched record
    except Exception:  # noqa: BLE001
        return None
    if not getattr(record, "ok", False):
        return None
    classes = _record_classes(record)
    if not classes:
        return None
    plan = _copy.deepcopy(expr._plan.model_dump(mode="json"))
    changed = False
    for arg in _iter_field_select_args(plan):
        sel = arg["value"]
        if _selector_hits(record, sel):
            continue  # this field selector already matches -- nothing to fix
        fixed = _repair_selector(sel, classes)
        if fixed and _selector_hits(record, fixed):  # the repair actually matches now
            log.info("    repaired field selector %r -> %r", sel, fixed)
            arg["value"] = fixed
            changed = True
    if not changed:
        return None
    from ...query.expr import Expr
    from ...query.plan import Plan

    return Expr(Plan.model_validate(plan), expr._client)


def _short_fail_reason(expr: Any, rows: "list[Any]", brief: Brief, doc: Any) -> str:
    """A ONE-LINE reason a query didn't extract cleanly -- for the log (the full, multi-line
    hint goes to the model, not the console). Keeps the retry trace legible."""
    good = _populated_rows(rows)
    if not good:
        n = _selector_match_count(_row_selector(expr), doc)
        return f"0 rows (record selector matched {n if n is not None else '?'})"
    empty = _empty_required_fields(good, brief)
    if empty:
        return f"required field(s) {', '.join(empty)} empty on every row"
    return f"{len(good)} row(s) but incomplete"


def _pager_confirmed(doc: Any, hint: "PaginationHint | None") -> bool:
    """Probe that the source REALLY paginates before baking a pager into the shipped blob: walk two
    pages with the hint's best pager (:func:`~webclient.core.document.paginate.pager_kwargs`) and confirm a genuine, DISTINCT second page exists. ``paginate``'s own clamp guard drops a
    page-2 that merely re-serves page one (an out-of-range clamp) and it stops on an empty/404, so a
    length ``>= 2`` means a working pager. A single page -- mislabelled paginated, a clamp, or an
    unreachable page two -- returns False, so the blob is shipped page-one-only rather than paging
    into nothing or duplicates. One extra fetch; a probe failure never breaks authoring."""
    from ...core.document.paginate import pager_kwargs

    try:
        kwargs = pager_kwargs(hint.best if hint is not None else None)
        if "click" in kwargs or "scroll" in kwargs:
            return False  # a load-more pager needs a held browser page: not probed, not baked
        return len(list(doc.paginate(**kwargs, max_pages=2))) >= 2
    except Exception:  # noqa: BLE001 - a probe must never break authoring
        return False


def _artifact_from(
    expr: Any, doc: Any, brief: Brief, candidate_url: str,
    resolve: "Resolve | None", bases: "list[str]", *, paginate: bool = False,
    hint: "PaginationHint | None" = None,
) -> "tuple[QueryArtifact, list[Any]]":
    """Test one authored query against the source and build its :class:`QueryArtifact` (the
    self-contained, runnable blob + validation verdict + timeliness flag). Shared by the
    one-shot and staged authors. The extraction is TESTED on the fetched page one only (fast);
    ``paginate`` bakes a ``.paginate(...)`` into the SHIPPED blob (its advance chosen from
    ``hint``) -- but only after :func:`_pager_confirmed` verifies a real second page, so a
    mislabelled or clamped source ships page one instead of paging into nothing. Returns
    ``(artifact, extracted_rows)``."""
    tested, rows = _test_query(expr, doc) if doc.ok else (False, [])
    good = _populated_rows(rows)
    missing = _empty_required_fields(good, brief)  # required leaves empty on every row
    tnote, stale = _timeliness(good, brief)  # over ALL rows; a FLAG, never a ship blocker
    if paginate and doc.ok and not _pager_confirmed(doc, hint):
        log.info("    pagination probe: no distinct second page -> shipping page one only")
        paginate = False  # don't bake a pager that pages into nothing / a clamp
    exe = _executable_query(expr, candidate_url, resolve, paginate=paginate, hint=hint)  # self-contained + runnable
    try:  # the visual step tree, from the VALID parsed plan (before/independent of testing)
        explain = exe.explain()
    except Exception:  # noqa: BLE001 - never let rendering the explain break authoring
        explain = ""
    art = QueryArtifact(
        blob=exe.to_blob(),
        describe=exe.describe(),
        explain=explain,
        plan=exe._plan.model_dump(mode="json"),
        tested=tested,
        complete=bool(tested and good and not missing),  # every required leaf populated
        row_count=len(good),
        sample=list(good[:5]),
        resolve=(resolve.model_dump(mode="json") if resolve is not None else {}),
        base_urls=bases,
        timeliness=tnote,
        stale=stale,
    )
    return art, rows


def _representative_sample(part_rows: "list[list[Any]]", limit: int = 5) -> "list[Any]":
    """A sample that shows EACH non-empty section: one row from every part in turn, then more
    in order, capped at ``limit`` -- so a combined query's sample surfaces both shapes (the
    single upcoming row AND an archived row), not just the first section's rows."""
    out: list[Any] = []
    for row in part_rows:  # first pass: one row from each part, in order
        if row and len(out) < limit:
            out.append(row[0])
    for rows in part_rows:  # second pass: fill from the remainder, in order
        for r in rows[1:]:
            if len(out) >= limit:
                return out
            out.append(r)
    return out


def _combined_artifact(
    exprs: "list[Any]", doc: Any, brief: Brief, candidate_url: str,
    resolve: "Resolve | None", bases: "list[str]",
) -> "tuple[QueryArtifact, list[Any], list[int]]":
    """Test each SECTION query against the ONE fetched source and build a single combined
    :class:`QueryArtifact` whose rows are the CONCATENATION of every section's rows. Each
    section becomes a self-contained runnable :class:`QueryPart`; the combined verdict
    (``complete`` / ``row_count`` / ``sample`` / ``timeliness``) is judged over the union, and
    the top-level ``blob``/``describe``/``explain`` describe the FIRST section. Returns
    ``(artifact, combined_good_rows, per_section_good_counts)`` -- the counts let the caller
    give per-section feedback on a section that matched nothing."""
    parts: list[QueryPart] = []
    part_goods: list[list[Any]] = []
    all_tested = True
    for expr in exprs:
        tested, rows = _test_query(expr, doc) if doc.ok else (False, [])
        good = _populated_rows(rows)
        all_tested = all_tested and tested
        exe = _executable_query(expr, candidate_url, resolve)  # self-contained + runnable
        try:  # the visual step tree, independent of testing
            explain = exe.explain()
        except Exception:  # noqa: BLE001 - never let rendering the explain break authoring
            explain = ""
        parts.append(QueryPart(blob=exe.to_blob(), describe=exe.describe(),
                               explain=explain, row_count=len(good)))
        part_goods.append(good)
    combined_good = [r for good in part_goods for r in good]
    missing = _empty_required_fields(combined_good, brief)  # required leaves empty across the union
    tnote, stale = _timeliness(combined_good, brief)  # over the union; a FLAG, never a blocker
    first = parts[0]
    art = QueryArtifact(
        blob=first.blob,
        describe=first.describe,
        explain=first.explain,
        plan={},  # a combined query has no single plan; each section's plan lives in its blob
        tested=all_tested,
        complete=bool(all_tested and combined_good and not missing),  # every required leaf populated
        row_count=len(combined_good),
        sample=_representative_sample(part_goods),
        resolve=(resolve.model_dump(mode="json") if resolve is not None else {}),
        base_urls=bases,
        timeliness=tnote,
        stale=stale,
        parts=parts,
    )
    return art, combined_good, [len(g) for g in part_goods]


class AuthoringError(Exception):
    """The author could not produce a valid query this turn (e.g. the model's reply didn't parse).
    :func:`write_query` catches it and retries with feedback -- distinct from an ``LlmError`` (a
    transport/API failure, which aborts)."""


class Author(abc.ABC):
    """The query-authoring ENGINE seam. ``write_query`` owns the test / repair / recency-retry /
    artifact orchestration; the author owns only "produce the next candidate query expressions".
    This is the ONE boundary Phase 6 swaps -- from :class:`_TextAuthor` (prompt the model for
    query CODE and parse it) to an index-loop author (the model picks record/field indexes and
    ``llm.query_agent.build_query`` assembles the query). The orchestration is engine-agnostic."""

    @abc.abstractmethod
    def author(self, follow_up: "str | None" = None) -> "list[Any]":
        """The next candidate as 1..N query exprs (one per section). ``follow_up`` is the feedback
        from the last rejected attempt (``None`` on the first). Raises :class:`AuthoringError` when
        it cannot produce a valid query, so ``write_query`` retries with feedback."""


class _TextAuthor(Author):
    """Authors by prompting the model for query CODE and parsing it (the current engine). The PAGE
    (guide + skeleton + brief) is the OPENING message; each retry sends only the short feedback. If
    the model keeps a conversation (an :class:`LlmClient` exposes ``.conversation()``), the page
    stays in context and is re-read from cache instead of re-submitted every attempt; a plain
    ``Callable[[str], str]`` has no memory, so the page is re-sent each turn (the fallback)."""

    def __init__(self, llm: LLM, opening: str) -> None:
        conv = getattr(llm, "conversation", None)
        self._chat: Any = conv() if callable(conv) else None
        self._llm = llm
        self._opening = opening
        self._opened = False

    def _send(self, follow_up: "str | None" = None) -> str:
        """One authoring turn -> the model's raw reply (the opening on the first turn, then only
        the feedback for a conversational model, else the opening re-sent with the feedback)."""
        if self._chat is not None:  # stateful: the page once, then just the follow-up
            msg = self._opening if not self._opened else (follow_up or "Try again.")
            self._opened = True
            return str(self._chat.send(msg))
        # stateless callable: no memory -> the page must ride along every turn
        return self._llm(self._opening if not follow_up else f"{self._opening}\n\n{follow_up}")

    def author(self, follow_up: "str | None" = None) -> "list[Any]":
        """Send the turn and parse the reply into 1..N section queries; an unparsable reply is an
        :class:`AuthoringError` so ``write_query`` retries with the "reply with ONLY query code"
        feedback."""
        reply = self._send(follow_up)
        try:
            return _parse_queries(reply)  # 1 wq.doc chain, or one per section (split on ---)
        except Exception as exc:  # noqa: BLE001 - unparsable query code -> a retryable authoring miss
            log.debug("      unparseable reply: %.200r", reply.strip())
            raise AuthoringError(str(exc)) from exc


def _query_loop_prompt(brief: Brief, obs: Any, follow_up: "str | None") -> str:
    """The per-round prompt for the index-based query policy: the numbered RECORD options (the
    repeated structures) and FIELD options (the chosen record's leaves) with the brief's required
    fields, the query + sample so far, and any feedback -- asking for a JSON pick BY NUMBER (never
    a selector)."""
    records = "\n".join(f"  R{e.index}: {e.name} (repeats {e.repeats})" for e in obs.records) or "  (none)"
    fields = "\n".join(f'  F{e.index}: {e.role} "{e.name}"' for e in obs.fields) or "  (none)"
    want = ", ".join(brief.fields) or "the dataset's fields"
    sofar = f"\n\nQuery so far:\n  {obs.query}\nSample rows so far:\n  {obs.sample[:3]}" if obs.query else ""
    fb = f"\n\nFeedback to address: {obs.error or follow_up}" if (obs.error or follow_up) else ""
    return (
        "You are building a data-extraction query by PICKING NUMBERS -- never write a CSS "
        "selector. Choose the REPEATED RECORD that holds the dataset, then map each required "
        f"field to one of that record's FIELDS.\n\nRequired fields: {want}\n\n"
        f"RECORDS (repeated structures -- pick the dataset):\n{records}\n\n"
        f"FIELDS (leaves of the first record -- one per required field):\n{fields}{sofar}{fb}\n\n"
        'Reply with ONLY JSON: {"record": <R-number or null>, "fields": {"<field name>": '
        '<F-number>, ...}, "done": <true|false>}. Set record on the first turn; add fields; set '
        "done=true once the sample has every required field."
    )


def _llm_query_policy(llm: LLM, brief: Brief, follow_up: "str | None") -> Any:
    """An index-query policy backed by ``llm``: each round it prompts with the record/field
    options + sample and parses a JSON ``{record, fields, done}`` into a
    :class:`~webclient.llm.query_agent.QueryDecision` -- the model reasons in NUMBERS, the loop
    builds the selectors."""
    from ...llm.query_agent import QueryDecision

    def policy(obs: Any) -> Any:
        data = _ask_json(llm, _query_loop_prompt(brief, obs, follow_up)) or {}
        rec = data.get("record")
        cols = {
            str(k): int(v) for k, v in (data.get("fields") or {}).items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        }
        return QueryDecision(
            record=int(rec) if isinstance(rec, (int, float)) and not isinstance(rec, bool) else None,
            fields=cols, done=bool(data.get("done")),
        )

    return policy


class _LoopAuthor(Author):
    """Authors by DRIVING THE INDEX QUERY LOOP (Phase 6): the model picks record/field NUMBERS
    from ``doc``'s element index and ``llm.query_agent.build_query`` assembles the durable
    ``select_all(record).extract(fields).project()`` query -- the model never writes a selector.

    Record detection is a HINT, not a requirement: the index engine is a best-effort FAST-PATH
    tried ONCE. It hands off to ``fallback`` (the text author) when it can't help -- immediately
    when ``record_options`` surfaces nothing (no wasted LLM calls on a doomed pick), or on the
    next ``write_query`` retry if its query was rejected. So the index path can only ADD queries,
    never block one: a page its detector can't classify degrades to the proven text engine."""

    def __init__(self, llm: LLM, doc: Any, brief: Brief, fallback: "Author | None" = None) -> None:
        self._llm = llm
        self._doc = doc
        self._brief = brief
        self._fallback = fallback
        self._tried = False

    def author(self, follow_up: "str | None" = None) -> "list[Any]":
        from ...core.document.element_index import record_options
        from ...core.document.html import tree
        from ...llm.query_agent import build_query
        from ...query.expr import from_blob

        # a retry means the index engine's one shot was rejected -> hand off to text for recovery.
        if self._tried and self._fallback is not None:
            return self._fallback.author(follow_up)
        self._tried = True

        # no record hint at all -> don't burn LLM calls on a doomed index loop; go straight to text.
        has_hint = bool(record_options(tree(self._doc))) if getattr(self._doc, "ok", True) else False
        if has_hint:
            run = build_query(self._doc, _llm_query_policy(self._llm, self._brief, follow_up))
            if run.blob and run.row_count > 0:
                return [from_blob(run.blob)]
        if self._fallback is not None:
            log.info("    index author had no usable query (detection weak) — using the text author")
            return self._fallback.author(follow_up)
        raise AuthoringError("the index query loop produced no query and no fallback was set")


def _make_author(
    llm: LLM, prompt: str, doc: Any, brief: Brief, *, engine: str = "text"
) -> Author:
    """Build the query author for a ``write_query`` run -- THE single Phase-6 seam. ``engine``
    selects it: ``"text"`` (the default) returns the :class:`_TextAuthor` (prompt the model for
    query code); ``"index"`` returns the :class:`_LoopAuthor` -- the index fast-path with the text
    author as its FALLBACK, so record detection is a hint that can only help. ``write_query``'s
    test / repair / retry / artifact orchestration is the same either way."""
    if engine == "index":
        return _LoopAuthor(llm, doc, brief, fallback=_TextAuthor(llm, prompt))
    return _TextAuthor(llm, prompt)


def _resolve_on_non_link(expr: Any) -> "str | None":
    """Detect the common model mistake of calling ``.resolve()`` on a VALUE rather than a link
    (``.attr("text").resolve()`` instead of ``.attr("href").resolve()``). Returns a targeted
    feedback message, or ``None`` if every resolve follows a link attr. Recurses into nested
    sub-plans (``extract`` columns, ``when`` branches). ``resolve`` on a root reference /
    ``reference(col)`` (no preceding attr) is fine."""

    def _msg(name: str) -> str:
        return (
            f'You called .resolve() on .attr("{name}") -- but .resolve() follows a LINK, and a '
            'value/text is not a URL. To READ a value, .attr("text") is the whole answer (do not '
            'resolve it). To FOLLOW a link, resolve its href: .select("a").attr("href").resolve(). '
            'Re-write the query.'
        )

    def scan(steps: "list[Any]") -> "str | None":
        last_attr: "str | None" = None  # the arg of the most recent attr(...) call, per (sub)plan
        for i, s in enumerate(steps):
            if s.kind == "get" and s.name == "attr":
                nxt = steps[i + 1] if i + 1 < len(steps) else None
                if nxt is not None and nxt.kind == "call" and nxt.args:
                    v = nxt.args[0].value
                    last_attr = v if isinstance(v, str) else None
            elif s.kind == "get" and s.name == "resolve":
                if last_attr is not None and last_attr not in ("href", "src", "action"):
                    return _msg(last_attr)
                last_attr = None  # a valid resolve on a link (or a root/reference resolve)
            elif s.kind == "get" and s.name in ("select", "select_all", "reference"):
                last_attr = None  # a new selection -> the previous attr no longer applies
            if s.kind == "call":  # recurse into sub-plan args/kwargs (extract cols / when branches)
                for a in [*s.args, *s.kwargs.values()]:
                    sub = getattr(a, "plan", None)
                    if sub is not None and (r := scan(list(sub.steps))) is not None:
                        return r
        return None

    return scan(list(getattr(expr._plan, "steps", [])))


def _should_retry_for_recency(art: QueryArtifact, attempt: int, tries: int) -> bool:
    """A COMPLETE query whose newest data looks stale is probably scoped to an ARCHIVED
    period (a hidden year tab, an old paginated page). Worth one more try for the most
    recent data -- but never a ship blocker, so only while attempts remain."""
    return art.stale and attempt < tries - 1


def _precheck_sections(exprs: "list[Any]") -> "tuple[str, str] | None":
    """The pre-test guards applied to EVERY section query before it's run: each must select
    its records (a query with no ``select``/``select_all`` extracts nothing) and none may call
    ``.resolve()`` on a value instead of a link. Returns ``(reason, follow_up)`` for the FIRST
    offending section (the reason names the section when there is more than one), or ``None``
    when every section passes."""
    multi = len(exprs) > 1
    for i, expr in enumerate(exprs):
        where = f"section {i + 1} " if multi else ""
        ops = {s.name for s in expr._plan.steps if s.kind == "get"}
        if not ({"select", "select_all"} & ops):
            return (
                f"{where}has no record selection (.select_all missing)".strip(),
                f"{'Section ' + str(i + 1) + ' of your reply' if multi else 'Your query'} had NO"
                " selection so it extracts nothing. Every section MUST select the repeating record"
                " with .select_all(...), pull each field with .extract(col=...), and END with"
                " .project(). Re-write it.",
            )
        bad = _resolve_on_non_link(expr)
        if bad is not None:
            return (f"{where}.resolve() called on a value, not a link".strip(), bad)
    return None


def _split_section_follow_up(empties: "list[int]", exprs: "list[Any]") -> str:
    """Feedback for a SPLIT query that ran but didn't fully land. Names the section(s) that
    matched 0 records (fix the selector, or keep it if that section is genuinely empty) or, when
    all sections have rows but the result is still incomplete, asks for more semantic record
    selectors. Always restates the ``---``-separated one-query-per-section contract."""
    contract = (" Reply with one wq.doc... chain per section, separated by a line containing"
                " only ---.")
    if empties:
        which = ", ".join(str(i) for i in empties)
        return (
            f"Your split query ran, but section(s) {which} matched 0 records. Fix that section's"
            " .select_all(...) record selector to match its rows. If that section is GENUINELY"
            " empty on the page (e.g. no upcoming events), keep it as is and leave the working"
            " sections unchanged." + contract
        )
    return ("Your split query did not extract the dataset. Re-write each section's .select_all(...)"
            " to a more semantic record selector -- don't just tweak the fields." + contract)


def _recency_follow_up(art: QueryArtifact) -> str:
    """The feedback that pushes the model toward the MOST RECENT data + the hidden-tabs pattern."""
    return (
        f"That query is COMPLETE, but the most recent data looks MISSING: {art.timeliness}. "
        "The records you selected are probably an ARCHIVED period. Sites keep the CURRENT period "
        "behind a control: YEAR TABS (an older year shows by default), a 'Latest' vs 'Archive' "
        "toggle, a category filter, or a paginated first page. In the skeleton look for clickable "
        "tab/filter controls (marked '← clickable'), pagination, and records marked '[xhr]' "
        "(loaded on demand). Re-write the query to capture the MOST RECENT records -- pick a "
        "record selector that is NOT scoped to one archived tab (select across all of them, or "
        "the current/latest tab). Is there more recent data than what you selected?"
    )


def write_query(
    candidate_url: str,
    brief: Brief,
    *,
    wc: WebClient,
    llm: LLM,
    browser: BrowserMode = "auto",
    paginated: bool = False,
    pagination_hint: "PaginationHint | None" = None,
    retries: int = 4,
    extra_urls: Sequence[str] = (),
    resolve: "Resolve | None" = None,
    doc: Any = None,
    recency: str = "",
    author_engine: str = "text",
) -> QueryArtifact | None:
    """Have the model author the DOCUMENT-level extraction from the page skeleton, test
    it against the fetched source (``from_blob`` + run -> it must extract DATA rows), and
    return the best :class:`QueryArtifact`. The stored ``blob`` is the SELF-CONTAINED
    executable query -- the extraction wrapped in ``reference(url).resolve(...)`` so it
    runs as is (:func:`_executable_query`). Retries with feedback on an invalid or
    non-extracting query -- the page is sent ONCE and each retry is a short follow-up when the
    model keeps a conversation (see :func:`_author`), else re-sent. ``paginated`` tells the
    author to capture the next-page link; ``extra_urls`` are further base URLs the same query
    also runs against; ``resolve`` bakes the fetch policy (browser tier) into the executable
    query. ``doc`` is the already-fetched source (from the flag-read step) -- reused so we
    don't re-fetch it. ``author_engine`` picks HOW the model authors: ``"text"`` (the default --
    it writes query code) or ``"index"`` (it picks record/field indexes and ``build_query``
    builds the selectors, so it never authors CSS); the test/validation is the same either way."""
    if doc is None:
        doc = wc.fetch(candidate_url, browser=browser, optional=True)
    skeleton = _skeleton_for(doc) if doc.ok else ""
    prompt = _query_prompt(brief, skeleton, paginated=paginated, recency=recency)
    bases = [candidate_url, *extra_urls]
    best: QueryArtifact | None = None
    best_complete: QueryArtifact | None = None  # a complete-but-STALE fallback (recency retries)
    attempts: list[str] = []  # why each rejected attempt was rejected, for the onboard output
    nudged_empty = False  # a split query with an empty section is nudged ONCE, then accepted
    author: Author = _make_author(llm, prompt, doc, brief, engine=author_engine)  # text | index (build_query)
    follow_up: "str | None" = None
    tries = retries + 1

    def _with_attempts(art: QueryArtifact) -> QueryArtifact:
        art.attempts = list(attempts)  # attach the rejection trail to the query we return
        return art
    for attempt in range(tries):
        tag = f"    query {attempt + 1}/{tries}"
        try:
            exprs = author.author(follow_up)  # the authoring seam: 1..N query exprs (Phase 6 swaps it)
        except LlmError as exc:  # a bad-request / exhausted-retry API error
            log.warning("%s: LLM call failed (%s)", tag, exc)
            break
        except AuthoringError as exc:  # unparsable/unusable query -> retry with feedback
            log.info("%s: reply was not a valid query (%s) — retrying", tag, exc)
            attempts.append(f"attempt {attempt + 1}: not a valid query ({exc})")
            follow_up = ("Your previous reply was not a valid query. Reply with ONLY query code --"
                         " one wq.doc... chain, or, for a split dataset, one chain PER section"
                         " separated by a line containing only ---. Nothing else.")
            continue
        # every SECTION must select its records, and none may resolve() a value -- reject before
        # it looks like a 0-row "success" (a targeted, per-section reason on a multi-part reply).
        pre = _precheck_sections(exprs)
        if pre is not None:
            reason, follow_up = pre
            log.info("%s: %s — retrying", tag, reason)
            attempts.append(f"attempt {attempt + 1}: {reason}")
            continue
        if len(exprs) > 1:  # a SPLIT dataset: test each section, concatenate, judge the UNION
            art, rows, counts = _combined_artifact(exprs, doc, brief, candidate_url, resolve, bases)
            empties = [i + 1 for i, c in enumerate(counts) if c == 0]
            if art.complete:
                if _should_retry_for_recency(art, attempt, tries):  # stale union -> push for recent
                    best_complete = art
                    attempts.append(f"attempt {attempt + 1}: split query complete but stale — {art.timeliness}")
                    log.info("%s: split query complete but STALE — retrying (%s)", tag, art.timeliness)
                    follow_up = _recency_follow_up(art)
                    continue
                if empties and not nudged_empty:  # working query in hand; nudge ONCE to fill the empty section
                    nudged_empty, best_complete = True, art
                    which = ", ".join(map(str, empties))
                    attempts.append(f"attempt {attempt + 1}: split query section(s) {which} matched 0 records")
                    log.info("%s: split query section(s) %s empty — nudging once", tag, which)
                    follow_up = _split_section_follow_up(empties, exprs)
                    continue
                note = f" (STALE: {art.timeliness})" if art.stale else ""
                log.info("%s: ✓ complete split query%s — %d row(s) across %d section(s)",
                         tag, note, art.row_count, len(exprs))
                return _with_attempts(art)
            best = best or art  # keep the first rebuildable split as a fallback (NOT complete)
            reason = (f"split query section(s) {', '.join(map(str, empties))} matched 0 records"
                      if empties else f"split query {_short_fail_reason(exprs[0], rows, brief, doc)}")
            attempts.append(f"attempt {attempt + 1}: {reason}")
            log.info("%s: %s — retrying", tag, reason)
            follow_up = _split_section_follow_up(empties, exprs)
            continue
        expr = exprs[0]
        art, rows = _artifact_from(expr, doc, brief, candidate_url, resolve, bases, paginate=paginated, hint=pagination_hint)
        if art.complete:
            if not _should_retry_for_recency(art, attempt, tries):
                note = f" (STALE flag: {art.timeliness})" if art.stale else ""
                log.info("%s: ✓ complete%s — %d row(s)", tag, note, art.row_count)
                return _with_attempts(art)
            best_complete = art  # complete but stale: keep it, but push for the most recent data
            attempts.append(f"attempt {attempt + 1}: complete but stale — {art.timeliness}")
            log.info("%s: complete but STALE — retrying for the most recent data (%s)",
                     tag, art.timeliness)
            follow_up = _recency_follow_up(art)
            continue
        # AUTO-REPAIR a near-miss field selector (a one-char class typo: widget vs widgets) by
        # swapping in the nearest real class present in the record, then re-validate.
        repaired = _repair_query(expr, doc)
        if repaired is not None:
            rart, _rrows = _artifact_from(repaired, doc, brief, candidate_url, resolve, bases, paginate=paginated, hint=pagination_hint)
            if rart.complete and not _should_retry_for_recency(rart, attempt, tries):
                note = f" (STALE flag: {rart.timeliness})" if rart.stale else ""
                log.info("%s: ✓ complete after auto-repairing a selector%s — %d row(s)",
                         tag, note, rart.row_count)
                return _with_attempts(rart)
            if rart.complete and rart.stale:  # repaired but stale -> keep as fallback, push recency
                best_complete = rart
                attempts.append(f"attempt {attempt + 1}: repaired + complete but stale — {rart.timeliness}")
                log.info("%s: repaired + complete but STALE — retrying for recent (%s)",
                         tag, rart.timeliness)
                follow_up = _recency_follow_up(rart)
                continue
            art = rart if rart.row_count > art.row_count else art  # keep the better fallback
        best = best or art  # keep the first rebuildable one as a fallback (NOT complete)
        # a concise reason on the console; the full, multi-line diagnostic hint goes to the model.
        reason = _short_fail_reason(expr, rows, brief, doc)
        attempts.append(f"attempt {attempt + 1}: {reason}")
        log.info("%s: %s — retrying", tag, reason)
        follow_up = (
            "That query did not extract the dataset. Produce a MATERIALLY DIFFERENT query --"
            " change the .select_all(...) RECORD selector to a more semantic anchor, don't just"
            f" tweak the fields.\n\nYour previous query was:\n{expr.describe()}\n\n"
            f"{_content_hint(expr, rows, brief, doc)}"
        )
    # a complete-but-stale query (recency retries didn't find fresher data) beats an incomplete
    # one: it's a working query, and staleness is a FLAG for the human review, not a blocker.
    if best_complete is not None:
        log.info("    keeping the complete query with a STALE flag (%s)", best_complete.timeliness)
        return _with_attempts(best_complete)
    if best is None:  # every attempt failed to author a usable query -- say so loudly
        log.warning("    could not author a working query in %d attempt(s)", tries)
        return None
    return _with_attempts(best)


