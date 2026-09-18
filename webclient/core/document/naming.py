"""Human-readable element names: a short label that says what an element IS.

System C of the stamp-stream substrate (see correlate.py / interactivity.py). Like
interactivity, the DERIVATION is pure and static (runs on the parsed tree); the user's
requirement is that a name is a STAMP (usable by any consumer -- internal selection,
output labeling, the skeleton), not skeleton-only. Wiring the name onto the stamp stream
is a later integration; this module is the pure namer + model.

A name is drawn from the most specific human-meaning signal available, in priority order:
``aria-label`` -> ``alt`` -> ``title`` -> ``placeholder`` -> a button's ``value`` -> short
visible text -> a low-entropy, WORD-LIKE ``id``/``name`` (humanized). A hashed/opaque id
(``x7f3a``, ``css-1a2b``) yields no name -- better none than noise.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

_WS = re.compile(r"\s+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")  # camelCase boundary
_SEP = re.compile(r"[-_./]+")
#: a token that looks like a build hash rather than a word (mixed case + digits, or a long
#: hex/opaque run) -- the inverse of "word-like".
_HASHISH = re.compile(r"^(?=.*\d)(?=.*[a-z])(?=.*[A-Z]).{5,}$|^[0-9a-f]{6,}$", re.I)

#: tags whose ``value`` attribute is a human label (a submit/button control).
_VALUE_TYPES = frozenset({"submit", "button", "reset"})


class DomName(BaseModel):
    """A short human-readable label for an element, and where it came from."""

    node_key: str = ""  # the data-wc-node id when known (stamp path); "" for a pure-static name
    label: str = ""
    source: str = ""  # "aria" | "alt" | "title" | "placeholder" | "value" | "text" | "id"

    def __bool__(self) -> bool:
        return bool(self.label)


def _tag(el: Any) -> str:
    return el.tag.lower() if isinstance(getattr(el, "tag", None), str) else ""


def _norm(s: str) -> str:
    return _WS.sub(" ", s).strip()


def _wordlike(s: str) -> bool:
    """Whether ``s`` reads as words, not an opaque/hashed token. Requires letters and no
    hash-shaped run."""
    s = s.strip()
    if len(s) < 2 or not any(c.isalpha() for c in s):
        return False
    return not any(_HASHISH.match(tok) for tok in _SEP.split(s) if tok)


def _humanize(s: str) -> str:
    """Turn an id/name token into words: split camelCase + ``-_./`` separators, lowercase,
    collapse. ``"readMore-btn"`` -> ``"read more btn"``."""
    spaced = _CAMEL.sub(" ", s)
    spaced = _SEP.sub(" ", spaced)
    return _norm(spaced).lower()


def name(el: Any, *, max_len: int = 60) -> "DomName | None":
    """The best human-readable label for ``el`` (``None`` if nothing meaningful). Pure and
    static -- no browser. ``max_len`` clips an over-long label."""
    get = el.get if hasattr(el, "get") else (lambda _k: None)
    node_key = get("data-wc-node") or ""

    def hit(label: "str | None", source: str, *, humanize: bool = False) -> "DomName | None":
        if not label:
            return None
        label = _humanize(label) if humanize else _norm(label)
        if not label or not _wordlike(label):
            return None
        return DomName(node_key=node_key, label=label[:max_len], source=source)

    tag = _tag(el)
    # 1-4: explicit accessible labels, in priority order.
    for attr, source in (("aria-label", "aria"), ("alt", "alt"), ("title", "title"),
                         ("placeholder", "placeholder")):
        got = hit(get(attr), source)
        if got:
            return got
    # 5: a button/submit control's value is its label.
    if tag == "button" or (tag == "input" and (get("type") or "").lower() in _VALUE_TYPES):
        got = hit(get("value"), "value")
        if got:
            return got
    # 6: short visible text (a leaf control like <button>Read more</button>).
    text = _norm("".join(el.itertext())) if hasattr(el, "itertext") else ""
    if 0 < len(text) <= max_len:
        got = hit(text, "text")
        if got:
            return got
    # 7: a word-like id / name attribute (humanized); a hashed id yields nothing.
    for attr in ("id", "name"):
        got = hit(get(attr), "id", humanize=True)
        if got:
            return got
    return None


__all__ = ["DomName", "name"]
