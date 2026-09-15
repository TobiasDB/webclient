"""Bundled LLM skills, shipped as package data under ``webclient/skills/``.

These are reference guides an agent can pull at runtime (also exposed as an MCP
tool). They are pure text -- no client, no fetch -- so they load with nothing but
the base install.
"""

from __future__ import annotations

from importlib.resources import files


def _skill(name: str) -> str:
    """The text of a bundled skill (``webclient/skills/<name>.md``)."""
    return files("webclient").joinpath(f"skills/{name}.md").read_text(encoding="utf-8")


def lazy_query_guide() -> str:
    """The "writing lazy web queries" skill: the query-syntax reference (the ``wq``
    roots, select/extract/filter/project, operators, blobs) an LLM needs to author a
    lazy extraction plan -- query syntax only, no client/fetch/browser knowledge."""
    return _skill("lazy-queries")


__all__ = ["lazy_query_guide"]
