"""The kernel is the bottom layer: it may import stdlib + pydantic and its own
submodules, and NOTHING from a sibling webclient layer (core / query / clients /
pipelines / signals / dom / ...). This test makes that boundary executable, so the
package split can't silently regrow an upward edge from the kernel.

It walks every module under ``webclient/kernel`` and flags any import -- at module
level, inside a function, or under ``TYPE_CHECKING`` -- that escapes the kernel:
a relative import climbing out of the package (``from ..x``), or an absolute
``webclient.<layer>`` import for a layer other than ``kernel``. Deferred imports are
checked too: a runtime hop into an upper layer is still a layering violation, just a
lazier one.
"""

from __future__ import annotations

import ast
import pathlib

KERNEL = pathlib.Path(__file__).resolve().parent.parent / "webclient" / "kernel"


def _escapes(node: ast.ImportFrom | ast.Import, module_file: pathlib.Path) -> "str | None":
    """The offending target if this import leaves ``webclient.kernel``, else ``None``."""
    if isinstance(node, ast.Import):
        for alias in node.names:
            name = alias.name
            if name == "webclient" or name.startswith("webclient.") and not name.startswith("webclient.kernel"):
                return name
        return None
    # ImportFrom
    if node.level == 0:  # absolute
        mod = node.module or ""
        if mod == "webclient" or (mod.startswith("webclient.") and not mod.startswith("webclient.kernel")):
            return mod
        return None
    # relative: level 1 = this package (kernel or a subpackage of it); level >= 2
    # climbs to webclient/ or above -> out of the kernel. A kernel SUBpackage (e.g.
    # kernel/policy/) using level 2 to reach a kernel sibling is still inside the
    # kernel, so resolve the target against the file's package and check the prefix.
    pkg_parts = module_file.relative_to(KERNEL.parent.parent).with_suffix("").parts[:-1]
    # pkg_parts starts with ("webclient", "kernel", ...)
    base = pkg_parts[: len(pkg_parts) - (node.level - 1)] if node.level - 1 <= len(pkg_parts) else ()
    target = ".".join((*base, node.module)) if node.module else ".".join(base)
    if not target.startswith("webclient.kernel") and target != "webclient.kernel":
        return f"{'.' * node.level}{node.module or ''} -> {target or '<root>'}"
    return None


def test_kernel_imports_nothing_above_it() -> None:
    offenders: list[str] = []
    for path in sorted(KERNEL.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                bad = _escapes(node, path)
                if bad is not None:
                    rel = path.relative_to(KERNEL.parent.parent)
                    offenders.append(f"{rel}:{node.lineno}: {bad}")
    assert not offenders, (
        "webclient.kernel must not import any sibling layer -- these escape it:\n  "
        + "\n  ".join(offenders)
    )
