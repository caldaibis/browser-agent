"""Shared isolation for durable runtime state touched by unit tests."""
from __future__ import annotations

import pytest

import src.store
import src.apply_sessions


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(src.store, "DB_PATH", tmp_path / "store.db")
    monkeypatch.setattr(src.apply_sessions, "SESSIONS_DIR", tmp_path / "apply_sessions")
