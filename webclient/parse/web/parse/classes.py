"""Class-token classification -- which CSS classes are stable semantic hooks vs generated noise.

Pure string functions shared by the skeleton, the record detector, and the element index. A good
selector anchors on a semantic class (``product-card``, ``price``); it must never anchor on a
utility (``mt-6``, ``flex``) or a hashed build class (``css-1a2b3c``), which change every build and
appear on unrelated elements. Deliberately conservative on the hash side: far worse to drop a real
hook than to keep a little noise.
"""

from __future__ import annotations

import re

#: CSS-in-JS / CSS-module prefixes -- always generated, never a stable hook.
_NOISE_PREFIX = (
    "css-",
    "sc-",
    "jsx-",
    "emotion-",
    "chakra-",
    "mui",
    "makestyles",
    "jss",
)
_HEX_SEG = re.compile(r"[0-9a-f]*[0-9][0-9a-f]*")
#: an emotion / styled-components generated class: lowercase letters and digits INTERLEAVED with no
#: separator (``e1d6xluq5``, ``ej9ium94``, ``e4wm5bw1``) -- a semantic name keeps its digits at the
#: end (``col12``) or behind a separator (``col-xs-6``).
_INTERLEAVED = re.compile(r"[a-z]+\d+[a-z]+[a-z0-9]*")
#: unambiguous bare Tailwind utilities (ambiguous words like ``container``/``block`` are spared).
_UTILITY_BARE = frozenset(
    {
        "flex",
        "grid",
        "hidden",
        "relative",
        "absolute",
        "fixed",
        "sticky",
        "inline-flex",
        "inline-block",
        "truncate",
        "italic",
        "uppercase",
        "lowercase",
        "capitalize",
        "underline",
        "antialiased",
        "transform",
        "transition",
    }
)
#: a Tailwind ``prop-value`` utility (``mt-6``/``px-4``/``text-center``/``bg-white``/``col-span-2``).
_UTILITY_PREFIX = re.compile(
    r"^-?(?:[mp][trblxyse]?|w|h|min-w|max-w|min-h|max-h|size|gap|gap-[xy]|space-[xy]|inset|top|"
    r"right|bottom|left|z|grid-cols|grid-rows|col-span|col-start|col-end|row-span|row-start|"
    r"row-end|order|basis|grow|shrink|flex|justify|items|self|content|place|text|font|leading|"
    r"tracking|indent|align|whitespace|break|bg|from|via|to|border|divide|rounded|ring|outline|"
    r"shadow|opacity|overflow|object|aspect|columns|float|clear|cursor|select|resize|scroll|snap|"
    r"transition|duration|ease|delay|animate|scale|rotate|translate|skew|origin|fill|stroke|sr)-\S+$"
)


#: a LABELLED generated class -- the emotion / styled-components / CSS-modules label form
#: ``<prefix>-<hash>-<ComponentName>`` (``ssrcss-evdvfk-StyledListItem``, ``css-1a2b3c-Button``):
#: the hash changes every build, the CapitalCase label is the stable part.
_LABELLED = re.compile(r"^[a-z]{2,8}-[a-z0-9]{5,10}-([A-Z][A-Za-z0-9]{2,})$")
#: the CSS-modules ``<Name>_<hash>`` form (``Button_a1B2c``, ``card-title_x9f3k``): the hash has a
#: digit; the name before it is the stable part.
_MODULE = re.compile(r"^([A-Za-z][A-Za-z0-9-]*?)_([A-Za-z0-9]*\d[A-Za-z0-9]*)$")


def stable_stem(tok: str) -> str:
    """The STABLE part of a generated class (``StyledListItem`` of ``ssrcss-evdvfk-StyledListItem``,
    ``Button`` of ``Button_a1B2c``) -- what a durable selector matches with ``[class*="..."]``;
    ``""`` when ``tok`` is not a recognised generated form (or has no label)."""
    m = _LABELLED.match(tok) or _MODULE.match(tok)
    return m.group(1) if m and len(m.group(1)) >= 3 else ""


def class_hook(tok: str) -> str:
    """``tok`` as a DURABLE selector fragment: ``.tok`` for a semantic class, ``[class*="Stem"]``
    for a generated class with a stable stem, ``""`` for noise (a utility / a stemless hash) -- so
    a caller composing a selector skips it."""
    stem = stable_stem(tok)
    if stem:
        return f'[class*="{stem}"]'
    return "" if is_noise_class(tok) else f".{tok}"


def is_utility_class(tok: str) -> bool:
    """A layout/spacing/typography utility (Tailwind & co.) that carries no record identity."""
    return tok in _UTILITY_BARE or bool(_UTILITY_PREFIX.match(tok))


def is_noise_class(tok: str) -> bool:
    """Whether ``tok`` is NOT a useful semantic hook -- a utility, a CSS-module/hashed build class
    (``css-1a2b3c``/``Button_a1B2c``), else kept (short/semantic names are spared)."""
    if is_utility_class(tok):
        return True
    if len(tok) < 5:
        return False
    if tok.lower().startswith(_NOISE_PREFIX):
        return True
    if (
        any(c.isupper() for c in tok)
        and any(c.islower() for c in tok)
        and any(c.isdigit() for c in tok)
    ):
        return True  # mixed-case + digit -> generated hash
    if len(tok) >= 7 and _INTERLEAVED.fullmatch(tok):
        return True  # letters / digits interleaved, no separator -> an emotion-style hash
    return any(len(seg) >= 8 and _HEX_SEG.fullmatch(seg) for seg in re.split(r"[-_]", tok))


def semantic_classes(classes: "list[str]") -> "list[str]":
    """The meaningful class tokens, dropping utility + hashed noise."""
    return [c for c in classes if not is_noise_class(c)]


__all__ = ["class_hook", "is_noise_class", "is_utility_class", "semantic_classes", "stable_stem"]
