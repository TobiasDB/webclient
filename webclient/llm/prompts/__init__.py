"""Onboarding-pipeline prompts, shipped as package data (``*.md`` alongside this
module).

Each ``.md`` file is a :class:`string.Template` -- ``$name`` placeholders filled at
render time. Keeping the prompts as data (rather than inline f-strings) lets them be
read, diffed and tuned without touching the pipeline code. Load + render one with
:func:`render_prompt`; templates are cached after first read.
"""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files
from string import Template


@lru_cache(maxsize=None)
def _template(name: str) -> Template:
    """The cached :class:`string.Template` for ``<name>.md`` in this package."""
    text = files(__name__).joinpath(f"{name}.md").read_text(encoding="utf-8")
    # Drop the file's trailing newline so a rendered prompt matches the exact
    # wording of a hand-written string (editors add one; the prompt does not want it).
    return Template(text.rstrip("\n"))


def render_prompt(name: str, /, **variables: str) -> str:
    """Render prompt ``name`` (a ``.md`` file in this package) with ``variables``.

    Every ``$placeholder`` in the template must be supplied; a missing or extra
    variable raises (via :meth:`string.Template.substitute`), so a typo in a prompt
    or a call site fails loudly rather than shipping a half-filled prompt.
    """
    return _template(name).substitute(variables)


__all__ = ["render_prompt"]
