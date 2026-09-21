"""``webclient.dom`` is the PURE toolkit: it must never import the cores, the engine, the
query machinery or the clients (D4 in the roadmap) -- so the static replay engine and a slim
remote install can use it without an ``Engine``."""

import ast
import pathlib

import webclient.dom

FORBIDDEN = ("webclient.core", "webclient.query", "webclient.clients", "webclient.events",
             "webclient.interface", "webclient.pipelines", "webclient.llm")


def _imports(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text())
    out: list[str] = []
    for node in tree.body:  # MODULE-LEVEL only: a lazy in-function lxml import is fine
        if isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level:  # a relative import -- resolve against the dom package
                mod = "webclient.dom" + ("." + mod if mod else "")
                if node.level > 1:
                    mod = "webclient" + ("." + (node.module or "") if node.module else "")
            out.append(mod)
    return out


def test_dom_imports_nothing_stateful():
    pkg = pathlib.Path(webclient.dom.__file__).parent
    for py in pkg.glob("*.py"):
        for mod in _imports(py):
            assert not mod.startswith(FORBIDDEN), f"{py.name} imports {mod}"
            assert mod not in ("lxml", "lxml.html", "lxml.etree"), f"{py.name} imports lxml at load"


def test_dom_public_api_is_stable():
    for name in ("norm", "tag", "text_of", "parse_html", "decode_html", "parse_json",
                 "json_leaves", "clean_href", "strip_wc_attrs", "visible_text"):
        assert hasattr(webclient.dom, name)
