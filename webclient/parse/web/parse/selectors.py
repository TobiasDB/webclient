"""CSS selector HYGIENE -- the deterministic rules that keep a written selector durable.

A selector names WHAT an element is, never where it sits or how a build happened to label it.
Two mechanical services, shared by the record detector's suggestions and by every place a
model-written selector enters the pipeline:

* :func:`normalise` rewrites what can be fixed without changing the meaning: the strict child
  combinator (``ul > li``) becomes a descendant (``ul li`` -- one inserted wrapper breaks the
  former), and a generated class with a stable label (``.ssrcss-evdvfk-StyledListItem``) becomes
  its stem match (``[class*="StyledListItem"]``). XPath and quoted attribute values are left alone.
* :func:`problems` names what is WRONG for the selector's ROLE: a record selector by id (an id
  names ONE element; a list of ids enumerates the sample) or by position (``:nth-child``); a
  listing field by id (unique on the page: every record reads the same element). A detail page is
  its own document, so an id there is a fine hook.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Literal

from .classes import class_hook, stable_stem

#: where a selector is used -- what hygiene applies.
Role = Literal["records", "field", "detail", "anchor"]

_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")
_CHILD = re.compile(r"\s*>\s*")
_CLASS = re.compile(r"\.(-?[_a-zA-Z][\w-]*)")
_ID = re.compile(r"#(-?[_a-zA-Z][\w-]*)")
_POSITIONAL = re.compile(
    r":(?:nth-(?:last-)?(?:child|of-type)\([^)]*\)|(?:first|last|only)-(?:child|of-type))"
)
_COMBINATOR = re.compile(r"\s*[>+~]\s*|\s+")


def is_xpath(selector: str) -> bool:
    """Whether ``selector`` is an XPath expression (left untouched by the CSS rules)."""
    return selector.lstrip().startswith(("/", "./", "(/", "(./"))


def _unquoted(selector: str, fix: "Callable[[str], str]") -> str:
    """``fix`` applied to the parts of ``selector`` OUTSIDE quotes (attribute values keep their
    dots / arrows: ``[href*=".pdf"]``)."""
    out: list[str] = []
    at = 0
    for m in _QUOTED.finditer(selector):
        out.append(fix(selector[at : m.start()]))
        out.append(m.group(0))
        at = m.end()
    out.append(fix(selector[at:]))
    return "".join(out)


def _durable_class(m: "re.Match[str]") -> str:
    tok = m.group(1)
    return f'[class*="{stable_stem(tok)}"]' if stable_stem(tok) else m.group(0)


def normalise(selector: str) -> str:
    """The selector with the strict child combinator relaxed to a descendant and each labelled
    generated class replaced by its stem match. Never empties a selector; XPath passes through."""
    if is_xpath(selector):
        return selector
    fixed = _unquoted(selector, lambda part: _CLASS.sub(_durable_class, _CHILD.sub(" ", part)))
    return fixed.strip() or selector


def _parts(selector: str) -> "list[str]":
    """The comma-separated alternatives of a selector list (commas inside quotes / parens kept)."""
    out: list[str] = []
    cur: list[str] = []
    depth, quote = 0, ""
    for ch in selector:
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    out.append("".join(cur).strip())
    return [p for p in out if p]


def _last_compound(part: str) -> str:
    """The compound selector that names the MATCHED element (after the last combinator)."""
    bare = _QUOTED.sub('""', part)
    pieces = [p for p in _COMBINATOR.split(bare.strip()) if p]
    return pieces[-1] if pieces else bare


def problems(selector: str, role: Role) -> "list[str]":
    """What is wrong with ``selector`` in ``role`` -- each a sentence that says what to do
    instead; empty when it passes. See the module docstring for the rules."""
    if is_xpath(selector) or role in ("detail", "anchor"):
        return []
    out: list[str] = []
    parts = _parts(selector)
    by_id = [p for p in parts if _ID.search(_last_compound(p))]
    if role == "records":
        if len(parts) > 1 and len(by_id) == len(parts):
            out.append(
                f"{selector!r} ENUMERATES records by id -- that selects the records you looked at, "
                "not the dataset; name the element that REPEATS (its tag + a semantic class, or an "
                "attribute every record shares)"
            )
        elif by_id:
            out.append(
                f"{selector!r} selects a record by id: an id names ONE element; a record selector "
                "must name the element that REPEATS (its tag / a semantic class / an attribute "
                "shared by every record)"
            )
        if any(_POSITIONAL.search(p) for p in parts):
            out.append(
                f"{selector!r} is positional (:nth-child / :first-child): a record selector names "
                "the repeating element, not a position in a list"
            )
    elif by_id:
        out.append(
            f"{selector!r} reads a field by id: an id is unique on the page, so every record would "
            "read the same element (or miss) -- read the field RELATIVE to the record by tag / "
            "class / attribute"
        )
    return out


__all__ = ["Role", "class_hook", "is_xpath", "normalise", "problems"]
