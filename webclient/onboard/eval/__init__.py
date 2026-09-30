"""The onboard eval harness: run Locate + Author against the bundled lab and grade each example.

Not part of the shipped ``web.onboard`` package -- a dev tool that imports both ``web.onboard`` and
the bundled ``eval.lab`` (the fixtures + their published expected results). Run it with
``python -m eval`` from ``webclient/onboard`` (both importable); see :mod:`eval.__main__`.
"""

from __future__ import annotations

from web.onboard import ClaudeShim

from .harness import CASES, Case, Result, run_all, run_case

__all__ = ["CASES", "Case", "Result", "run_all", "run_case", "ClaudeShim"]
