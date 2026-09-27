"""onboarding.query_build -- parse a model reply into a query and DETERMINISTICALLY wrap it.

The safe AST allowlist that rebuilds a written ``wq.doc`` chain through our own interface (never
``eval``), the split-into-sections parse, selector normalisation, and the reference+resolve(+paginate)
wrapping that makes an authored extraction a self-contained, runnable blob."""

from typing import Any

from ...interface import wq
from ...query.expr import from_blob
from ...core.document.models import PaginationHint
from ...policy import Resolve
from .llm import _strip_fences, _json_blob


#: typographic characters a model sometimes emits INSTEAD of the ASCII code punctuation, which
#: then breaks ``ast.parse`` (e.g. a smart quote in a selector, an em-dash in a range). Query code
#: only ever wants the ASCII forms, so normalising these is safe and stops a needless retry.
_SMART_PUNCT = {"“": '"', "”": '"', "‘": "'", "’": "'",
                "–": "-", "—": "-", "…": "...", " ": " "}


def _query_code(reply: str) -> str:
    """The query EXPRESSION from the model's reply: strip any code fence / prose and start
    at the first ``wq.`` so a leading ``query =`` assignment or preamble is dropped, cut a
    trailing code fence (a model that wraps the code in ``` despite the ask), and normalise the
    typographic punctuation a model sometimes emits (smart quotes / em-dash) to the ASCII forms
    the query wants, so a stray “ or — doesn't fail parsing and burn a retry."""
    t = _strip_fences(reply)
    i = t.find("wq.")
    if i != -1:
        t = t[i:]
    fence = t.find("```")  # a trailing fence when prose preceded the opening one
    if fence != -1:
        t = t[:fence]
    for bad, good in _SMART_PUNCT.items():
        if bad in t:
            t = t.replace(bad, good)
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

def _paginate_steps(max_pages: int = 50, mode: Any = None, records: str = "") -> list[Any]:
    """The plan steps for the ``.paginate(...)`` a single confirmed pager ``mode`` (a
    :class:`~.models.PagerHint`: next link / page param / load-more) describes, spliced between the
    reference resolve and the extraction so the shipped query walks the dataset's pages and the body
    extracts across all of them. ``mode`` None: follow ``rel=next`` / the HTTP Link header. Authoring
    still tests page one only. ``records`` (the record selector) is passed to ``paginate`` so a
    repeated page is recognised by its RECORD texts, not a content hash: a ``?page=`` param the server
    IGNORES, or a Next link that loops back to page one, is detected as a repeat and the walk stops."""
    from ...core.document.paginate import pager_kwargs

    kwargs = pager_kwargs(mode)
    if records:
        kwargs["records"] = records
    plan = wq.doc.paginate(**kwargs, max_pages=max_pages)
    return list(plan._plan.steps)

def _executable_query(
    doc_expr: Any, url: str, resolve: "Resolve | None", *, paginate: bool = False, max_pages: int = 50,
    mode: Any = None,
) -> Any:
    """DETERMINISTICALLY wrap the model's DOCUMENT-level extraction into a SELF-CONTAINED
    query rooted at the source reference with a ``resolve`` step baked in, so
    ``from_blob(blob).collect()`` fetches + resolves + extracts with no context --
    executable exactly as output. The model supplies only the extraction; this function
    (no LLM) supplies the reference + resolve. When the source needs proxy / antibot, the
    FULL policy is baked in (``resolve(policy=...)``) so the blob re-fetches with it; a
    plain source just bakes the browser tier. ``paginate`` splices the CONFIRMED pager ``mode``'s
    ``.paginate(...)`` after the resolve, so a paginated source's blob pulls the WHOLE dataset."""
    from ...query.expr import Expr
    from ...query.plan import Plan

    ref = wq.reference(url)
    if resolve is not None and (resolve.proxy is not None or resolve.antibot is not None):
        rooted = ref.resolve(policy=resolve.model_dump(mode="json"))  # full policy in the blob
    else:
        tier = resolve.browser.when if (resolve is not None and resolve.browser is not None) else None
        rooted = ref.resolve(browser=tier) if tier else ref.resolve()
    records = ""
    if paginate:  # tell paginate the RECORD selector so it dedups by records, not a content hash
        from .query_diagnose import _row_selector
        records = _row_selector(doc_expr) or ""
    pag = _paginate_steps(max_pages, mode, records) if paginate else []
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
