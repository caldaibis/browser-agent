"""Shared test isolation.

Every test gets a throwaway SQLite store: production code dual-writes
processed listings / incidents to `state/store.db`, and without this any
test that exercises those paths would read from — and write into — the real
state directory. (autouse fixtures apply to unittest.TestCase tests too.)
"""
from __future__ import annotations

import pytest

import src.store


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(src.store, "DB_PATH", tmp_path / "store.db")
    monkeypatch.setattr(src.store, "LEGACY_PROCESSED", tmp_path / "processed_listings.jsonl")
    monkeypatch.setattr(src.store, "LEGACY_INCIDENTS", tmp_path / "incidents.jsonl")
