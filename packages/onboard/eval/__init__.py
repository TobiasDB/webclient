"""The onboard eval harness: run Locate + Author against the webclient lab and grade each example.

Not part of the shipped ``web.onboard`` package -- a dev tool that imports both ``web.onboard`` and
the OLD repo's ``webclient.lab`` (the fixtures + their published expected results). Run it with
``python -m eval`` from ``packages/onboard`` (both importable); see :mod:`eval.__main__`.
"""

from __future__ import annotations

from .harness import CASES, Case, Result, run_all, run_case
from .heuristic_llm import HeuristicLlm

__all__ = ["CASES", "Case", "Result", "run_all", "run_case", "HeuristicLlm"]
