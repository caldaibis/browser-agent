"""Shared test isolation for the authoritative SQLite state store."""
from __future__ import annotations

import pytest

import src.store


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(src.store, "DB_PATH", tmp_path / "store.db")
