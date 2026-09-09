"""The typing gate: authoring a plan must type-check under --strict.

This is what earns the single-class design. Declaring `attr -> str` would make
`.alias()` an error on `str` in the most common idiom in the language, so the
wrappers have to be real — and that has to stay true.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "typing_surface.py"


@pytest.mark.skipif(shutil.which("mypy") is None
                    and not (Path(sys.executable).parent / "mypy").exists(),
                    reason="mypy not installed")
def test_plan_authoring_type_checks():
    mypy = Path(sys.executable).parent / "mypy"
    result = subprocess.run(
        [str(mypy), "--strict", "--follow-imports=silent", str(FIXTURE)],
        capture_output=True, text=True, cwd=Path(__file__).parent.parent)
    assert result.returncode == 0, result.stdout + result.stderr
