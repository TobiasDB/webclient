"""Core object model: the surface classes, the error/capability policy, and
the backings. Isolated from the rest of the package (engine, plugins,
service, remote) -- they depend on core, not the reverse."""
from .base import (CLASSES, IGNORE, RAISE, RETURN, Capability, ErrorPolicy,
                  OpError, UnsupportedOperation, default_policy, now, policy)
from ..document import (Collection, Document, Element, FetchError, Field,
                        HttpMethod, LiveDocument, LiveNode, Reference,
                        Script, WebBase, WebError, _is_xpath, _json_path,
                        _select_elements)
from .document import DocumentCore
from .webclient import Proxy
from . import backings

__all__ = ["CLASSES", "IGNORE", "RAISE", "RETURN", "Capability", "ErrorPolicy",
           "OpError", "UnsupportedOperation", "default_policy", "now", "policy",
           "Collection", "Document", "Element", "FetchError", "Field",
           "HttpMethod", "LiveDocument", "LiveNode", "Proxy", "Reference",
           "Script", "WebBase", "WebError", "backings", "DocumentCore"]
