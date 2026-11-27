"""Shared fixtures for every test."""

from __future__ import annotations

import pytest

from hazelst.sources import elevation


@pytest.fixture(autouse=True)
def no_elevation_pacing(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the pauses between elevation batches instead of sleeping."""
    pauses: list[float] = []
    monkeypatch.setattr(elevation, "sleep", pauses.append)
    return pauses
