"""Shared fixtures for the onboard tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every test writes its located-reference cache and its verb record (``verbs.jsonl``) under
    a throw-away ``XDG_CACHE_HOME`` -- never the developer's real ``~/.cache/web-onboard`` (the
    CLI tests run ``main()`` for real, and used to leave dozens of test lines in the cross-run
    verb record)."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
