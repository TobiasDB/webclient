"""Stop conditions for pagination -- composable, and stateful when they need to be.

An ``until`` is a plain ``Callable[[Document], bool]``: return True to stop after this page. An
empty page is only one stop of many -- real ones are frequently STATEFUL across pages (an item
budget, a loop guard, an id/date cutoff), so these builders return closures that carry state.
Pagination also stops on the FIRST of several conditions, so :func:`any_of` combines them.

    until = any_of(until_empty(".row"), first_n(200, ".row"), until_repeat())
"""

from __future__ import annotations

from collections.abc import Callable

from web.parse import Document

from .paginate import Until


def until_empty(selector: str) -> Until:
    """Stop when a page matches no items -- the classic 'ran off the end'."""
    return lambda doc: not doc.select_all(selector)


def until_match(selector: str) -> Until:
    """Stop when a marker appears -- a 'last page' / disabled-next element (the dual of empty)."""
    return lambda doc: doc.select(selector) is not None


def first_n(n: int, selector: str) -> Until:
    """Stop once the CUMULATIVE count of ``selector`` items reaches ``n`` (an item budget).
    Stateful -- counts as it goes and stops on the page that crosses ``n``."""
    seen = 0

    def stop(doc: Document) -> bool:
        nonlocal seen
        seen += len(doc.select_all(selector))
        return seen >= n

    return stop


def until_repeat(key: "Callable[[Document], str] | None" = None) -> Until:
    """Loop guard: stop when a page's ``key`` repeats (a cursor that stopped advancing, a feed
    that wraps). ``key`` defaults to the page text. Stateful."""
    k = key or (lambda d: d.text)
    seen: set[str] = set()

    def stop(doc: Document) -> bool:
        v = k(doc)
        if v in seen:
            return True
        seen.add(v)
        return False

    return stop


def any_of(*stops: Until) -> Until:
    """Stop on the FIRST of several conditions. Evaluates EVERY stop each page (not short-circuit),
    so stateful stops stay accurate even when another fires first."""
    return lambda doc: any([s(doc) for s in stops])


__all__ = ["until_empty", "until_match", "first_n", "until_repeat", "any_of"]
