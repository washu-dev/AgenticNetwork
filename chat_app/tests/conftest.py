"""Shared setup for the chat app tests."""

from __future__ import annotations

import pytest

from agentnet import settings


@pytest.fixture(autouse=True)
def _no_database(monkeypatch) -> None:
    """Start every test DB-less, whatever the caller's shell exports (a DATABASE_URL
    set for another project would put the app in DB mode). A test that wants a
    database sets DATABASE_URL itself."""
    for name in (settings.DATABASE_URL, *settings.OLD_DATABASE_NAMES):
        monkeypatch.delenv(name, raising=False)
    settings.reset_warnings()
