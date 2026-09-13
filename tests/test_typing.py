"""The typing gate: tests/fixtures/typing_surface.py must pass both checkers.
Skips a checker that is not installed (dev extra installs both)."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "typing_surface.py"
BIN = Path(sys.executable).parent


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(not (BIN / "mypy").exists(), reason="mypy not installed")
def test_surface_type_checks_under_mypy_strict():
    _run([str(BIN / "mypy"), "--strict", "--follow-imports=silent", str(FIXTURE)])


@pytest.mark.skipif(not (BIN / "pyright").exists(), reason="pyright not installed")
def test_surface_type_checks_under_pyright():
    _run([str(BIN / "pyright"), str(FIXTURE)])


def test_collection_stub_is_generated_from_the_registry():
    """Decision 2: the Collection[T] twin is generated, never hand-edited."""
    _run([sys.executable, str(ROOT / "scripts" / "gen_stubs.py"), "--check"])
