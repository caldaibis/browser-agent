from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request

from src import apply_sessions
from src.dashboard import app, data


def _request(*, htmx: bool = False) -> Request:
    headers = [(b"hx-request", b"true")] if htmx else []
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


def test_live_transcript_snapshot_is_complete_and_path_confined(tmp_path):
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    transcript = transcripts / "run.log"
    transcript.write_text("first\nsecond\n", encoding="utf-8")
    outside = tmp_path / "outside.log"
    outside.write_text("private", encoding="utf-8")

    with patch.object(data, "TRANSCRIPTS_DIR", transcripts):
        text, offset = data.live_transcript_snapshot(str(transcript))
        assert text == "first\nsecond\n"
        assert offset == len(text.encode())
        assert data.live_transcript_chunk(str(transcript), len("first\n")) == (
            b"second\n", offset,
        )
        assert data.live_transcript_snapshot(str(outside)) == ("", 0)
        transcript.write_bytes(b"first\npartial")
        assert data.live_transcript_snapshot(str(transcript)) == ("first\n", 6)


def test_finished_session_stream_replays_each_complete_message(tmp_path):
    sessions = tmp_path / "sessions"
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    transcript = transcripts / "run.log"
    transcript.write_text("12:00:00 [agent] first\n12:00:01 [agent] second\n", encoding="utf-8")

    with patch.object(apply_sessions, "SESSIONS_DIR", sessions), \
         patch.object(data, "TRANSCRIPTS_DIR", transcripts):
        session = apply_sessions.create_session(source_url="https://rental.test/1")
        apply_sessions.finish_session(
            session.id,
            outcome="submitted",
            transcript_path=str(transcript),
        )
        response = app.live_session_events(session.id, offset=0)

        async def consume() -> str:
            chunks: list[str] = []
            async for chunk in response.body_iterator:
                chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
            return "".join(chunks)

        body = asyncio.run(consume())
        assert "event: status" in body
        assert body.count("event: message") == 2
        assert 'data: "12:00:00 [agent] first"' in body
        assert "event: complete" in body


def test_retry_redirects_htmx_to_precreated_live_session(tmp_path):
    sessions = tmp_path / "sessions"
    logs = tmp_path / "logs"
    logs.mkdir()
    lock = tmp_path / "state" / "apply.lock"
    lock.parent.mkdir()
    url = "https://www.stekkies.com/listing/abc123"

    with patch.object(apply_sessions, "SESSIONS_DIR", sessions), \
         patch.object(app, "APPLY_LOCK", lock), \
         patch.object(app, "LOG_DIR", logs), \
         patch.object(app.store, "delete_processed"), \
         patch.object(app.subprocess, "Popen") as popen:
        response = app.action_retry(_request(htmx=True), url=url)
        location = response.headers["hx-redirect"]
        session_id = Path(location).name
        created = apply_sessions.get_session(session_id)
        assert created is not None
        assert created.retry
        assert created.stekkies_url == url

    assert location == f"/session/{session_id}"
    command = popen.call_args.args[0][-1]
    assert f"--session-id {session_id}" in command
    assert lock.exists()


def test_retry_lock_refusal_does_not_remove_dedup_state(tmp_path):
    lock = tmp_path / "apply.lock"
    lock.write_text("busy", encoding="utf-8")
    with patch.object(app, "APPLY_LOCK", lock), \
         patch.object(app.store, "delete_processed") as delete:
        response = app.action_retry(
            _request(htmx=True),
            url="https://www.stekkies.com/listing/abc123",
        )
    assert response.status_code == 200
    assert b"already running" in response.body
    delete.assert_not_called()
