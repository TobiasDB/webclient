"""Class-token classification: which CSS classes are stable semantic hooks and which are
utility / hashed-build noise. Pure string functions shared by the skeleton, the record
detector, the element index and the signals.
"""

from __future__ import annotations

import re

__all__ = ["is_utility_class", "is_noise_class", "semantic_classes"]

#: CSS-in-JS / CSS-module class prefixes -- always generated, never a stable selector hook.
_NOISE_CLASS_PREFIX = ("css-", "sc-", "jsx-", "emotion-", "chakra-", "mui", "makestyles", "jss")
_HEX_SEG = re.compile(r"[0-9a-f]*[0-9][0-9a-f]*")  # hex chars incl. at least one digit


#: bare Tailwind display/flex/text-transform utilities that carry NO record meaning. Kept to the
#: UNAMBIGUOUS ones -- ambiguous common words that could be a real hook (``container``/``block``/
#: ``inline``/``static``/``border``/``rounded``/``shadow``) are spared here; their dashed forms
#: (``border-2``/``rounded-lg``/…) are still caught by ``_UTILITY_PREFIX``.
_UTILITY_BARE = frozenset({
    "flex", "grid", "hidden", "relative", "absolute", "fixed", "sticky",
    "inline-flex", "inline-block", "flow-root",
    "truncate", "italic", "not-italic", "uppercase", "lowercase", "capitalize", "underline",
    "line-through", "no-underline", "antialiased", "transform", "transition",
})
#: a Tailwind-style ``prop-value`` utility (``mt-6``/``px-4``/``text-center``/``bg-white``/…).
#: Deliberately keyed on a KNOWN utility prop + a ``-value`` suffix, so a semantic ``feed-item``
#: / ``post-title`` / ``sold-out`` (prop is not a utility) and bare ``row``/``col``/``card``
#: (Bootstrap-semantic, no suffix) are spared. Grid ``col-span-2``/``row-start-1`` ARE stripped.
_UTILITY_PREFIX = re.compile(
    r"^-?(?:"
    r"[mp][trblxyse]?|w|h|min-w|max-w|min-h|max-h|size|"
    r"gap|gap-[xy]|space-[xy]|inset|inset-[xy]|top|right|bottom|left|z|"
    r"grid-cols|grid-rows|col-span|col-start|col-end|row-span|row-start|row-end|"
    r"order|basis|grow|shrink|flex|justify|justify-items|justify-self|items|self|content|place|"
    r"text|font|leading|tracking|indent|align|whitespace|break|"
    r"bg|from|via|to|border|divide|rounded|ring|outline|shadow|opacity|mix-blend|"
    r"overflow|overscroll|object|aspect|columns|float|clear|"
    r"cursor|select|resize|scroll|snap|touch|pointer-events|"
    r"transition|duration|ease|delay|animate|"
    r"scale|rotate|translate|skew|origin|"
    r"fill|stroke|sr"
    r")-\S+$"
)


def is_utility_class(tok: str) -> bool:
    """Whether a class is a layout/spacing/typography UTILITY (Tailwind & co.) that carries no
    record identity -- so it should never anchor a selector or split a record signature."""
    return tok in _UTILITY_BARE or bool(_UTILITY_PREFIX.match(tok))


def is_noise_class(tok: str) -> bool:
    """Whether a class token is NOT a useful semantic hook -- either a HIGH-ENTROPY generated name
    (a CSS-module / hashed build class like ``css-1a2b3c`` / ``jsx-1837462`` / ``Button_a1B2c``) or
    a layout/spacing/typography UTILITY (``mt-6`` / ``flex`` / ``px-4`` / ``text-center``). Kept
    deliberately CONSERVATIVE for the HASH half -- far worse to drop a real hook than keep noise --
    so it spares numbered / PascalCase semantic names (``heading2``/``ProductCardItem``/``USMap``)
    and Bootstrap-semantic bare words (``row``/``col``/``card``/``btn``); the utility half is keyed
    on known utility props so ``feed-item``/``post-title``/``sold-out`` are spared too. Stripping
    utilities is what stops a stray ``mt-6`` on one record from splitting its sibling group."""
    if is_utility_class(tok):  # checked BEFORE the length guard -- utilities are often < 5 chars
        return True
    if len(tok) < 5:
        return False  # short classes are almost always meaningful (nav, btn, col, row, h1)
    if tok.lower().startswith(_NOISE_CLASS_PREFIX):
        return True
    has_upper = any(c.isupper() for c in tok)
    has_lower = any(c.islower() for c in tok)
    has_digit = any(c.isdigit() for c in tok)
    if has_upper and has_lower and has_digit:
        return True  # mixed-case AND a digit -> a generated hash, never a hand-written class
    for seg in re.split(r"[-_]", tok):  # a bare hex hash segment (emotion/styled hashes)
        if len(seg) >= 8 and _HEX_SEG.fullmatch(seg):
            return True
    return False


def semantic_classes(classes: "list[str]") -> "list[str]":
    """The meaningful class tokens, dropping high-entropy generated ones (:func:`is_noise_class`)."""
    return [c for c in classes if not is_noise_class(c)]
