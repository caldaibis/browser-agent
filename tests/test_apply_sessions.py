from __future__ import annotations

from unittest.mock import patch

import pytest

from src import apply_sessions


def test_session_lifecycle_is_durable_and_live(tmp_path):
    with patch.object(apply_sessions, "SESSIONS_DIR", tmp_path):
        session = apply_sessions.create_session(
            stekkies_url="https://www.stekkies.com/listing/abc",
            retry=True,
        )
        assert session.live
        assert apply_sessions.get_session(session.id) == session

        with patch.object(apply_sessions.os, "kill") as kill:
            running = apply_sessions.claim_session(session.id, phase="resolving")
            assert running.live
            kill.assert_called_once_with(running.pid, 0)

        finished = apply_sessions.finish_session(
            session.id,
            outcome="submitted",
            transcript_path="/tmp/example.log",
        )
        assert not finished.live
        assert finished.status == "finished"
        assert finished.outcome == "submitted"
        assert apply_sessions.list_sessions(live_only=True) == []


def test_session_ids_and_updates_are_bounded(tmp_path):
    with patch.object(apply_sessions, "SESSIONS_DIR", tmp_path):
        session = apply_sessions.create_session()
        with pytest.raises(ValueError, match="unknown apply session fields"):
            apply_sessions.update_session(session.id, arbitrary_path="secret")
        assert apply_sessions.get_session("../outside") is None


def test_failed_session_preserves_registered_transcript(tmp_path):
    with patch.object(apply_sessions, "SESSIONS_DIR", tmp_path):
        session = apply_sessions.create_session()
        apply_sessions.update_session(
            session.id,
            transcript_path="/tmp/partial.log",
        )

        failed = apply_sessions.finish_session(
            session.id,
            error="RuntimeError: agent stopped",
        )

        assert failed.status == "failed"
        assert failed.transcript_path == "/tmp/partial.log"
