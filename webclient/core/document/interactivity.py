"""Back-compat shim: interactivity detection now lives in :mod:`webclient.dom.interactivity`."""

from ...dom.interactivity import DomInteractive, interactive, merge  # noqa: F401

__all__ = ["DomInteractive", "interactive", "merge"]
