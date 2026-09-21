"""``webclient.dom`` -- the pure, stateless toolkit for analysing, cleaning and extracting
from parsed web content (HTML / XML / JSON).

Every function here takes an element / string / bytes / parsed value and returns a value:
no ``webclient.core`` imports, no state, nothing cached. A core that wants a *cached* parse
(``Document._tree``) wraps :func:`parse_html`; the caching is the core's, the parsing is here.
lxml is OPTIONAL (the ``local`` extra): nothing imports it at module load, so ``signals`` and
a slim remote install can import this package freely.

Submodules (import what you need; the parse/json primitives are re-exported here):

* :mod:`.parse` -- decode / parse HTML, whitespace + tag helpers, href cleaning.
* :mod:`.json` -- lenient JSON parse, leaf walk, and the JSON shape skeleton.
* :mod:`.classes` -- semantic vs utility / hashed class tokens.
* :mod:`.markdown` -- tree -> markdown / readable text.
* :mod:`.skeleton` -- the token-lean DOM skeleton + element signatures.
* :mod:`.records` -- repeating-record (dataset) region detection.
* :mod:`.index` -- the numbered element table + durable selectors.
* :mod:`.naming` -- human-readable element names.
* :mod:`.interactivity` -- static interactivity detection.
* :mod:`.landmarks` -- nav / main / footer … region classification.
* :mod:`.select` -- CSS / XPath selection scoped to a node.
* :mod:`.regex` -- regex extraction over a value.
"""

from __future__ import annotations

from .json import json_leaves, json_skeleton, parse_json
from .parse import (
    clean_href,
    decode_html,
    local_name,
    norm,
    parse_html,
    sniff_charset,
    strip_wc_attrs,
    tag,
    text_of,
    visible_text,
)

__all__ = [
    "norm", "tag", "local_name", "text_of",
    "sniff_charset", "decode_html", "parse_html",
    "visible_text", "strip_wc_attrs", "clean_href",
    "parse_json", "json_leaves", "json_skeleton",
]
