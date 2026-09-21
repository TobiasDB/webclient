"""Back-compat shim: element naming now lives in :mod:`webclient.dom.naming`."""

from ...dom.naming import DomName, _wordlike, name  # noqa: F401

__all__ = ["DomName", "name"]
