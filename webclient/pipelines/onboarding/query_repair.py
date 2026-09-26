"""onboarding.query_repair -- deterministically repair a near-miss FIELD selector.

A conservative, high-confidence class-token swap (``widget`` -> the real ``widgets`` present in the
matched record), applied only when the repaired selector then actually matches -- so a one-char typo
doesn't sink an otherwise-correct query. The repaired query is re-validated downstream."""

from typing import Any

from ...interface import wq
from .common import log
from .query_build import _iter_field_select_args
from .query_diagnose import _row_selector, _selector_match_count


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
