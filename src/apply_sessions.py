"""Durable live-state registry for autonomous application sessions.

The transcript remains the append-only evidence for what the agent did.  This
module only owns the small piece of current state needed to answer "which runs
are live, and which transcript belongs to each one?" across the orchestrator
and dashboard processes.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, fields
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT
from .eventlog import parse_ts, utc_now_iso
from .redaction import redact

SESSIONS_DIR = PROJECT_ROOT / "state" / "apply_sessions"
_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}$")
_ACTIVE_STATUSES = {"queued", "running"}
_QUEUED_GRACE_SECONDS = 120


@dataclass(frozen=True)
class ApplySession:
    id: str
    created_ts: str
    updated_ts: str
    status: str = "queued"
    phase: str = "queued"
    pid: int = 0
    stekkies_url: str = ""
    source_url: str = ""
    source: str = ""
    address: str = ""
    transcript_path: str = ""
    outcome: str = ""
    error: str = ""
    retry: bool = False

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> ApplySession:
        allowed = {f.name for f in fields(cls)}
        values = {key: value for key, value in raw.items() if key in allowed}
        return cls(**values)

    @property
    def live(self) -> bool:
        if self.status not in _ACTIVE_STATUSES:
            return False
        if self.pid > 0:
            try:
                os.kill(self.pid, 0)
                return True
            except OSError:
                return False
        created = parse_ts(self.created_ts)
        if created is None:
            return False
        return (datetime.now() - created).total_seconds() <= _QUEUED_GRACE_SECONDS


def _session_path(session_id: str) -> Path:
    if not _ID_RE.fullmatch(session_id):
        raise ValueError("invalid apply session id")
    return SESSIONS_DIR / f"{session_id}.json"


def _safe_values(values: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in values.items():
        # This is an internal canonical path, not transcript content. Mutating
        # it when a credential username happens to match a directory component
        # would detach the live session from its transcript.
        if key == "transcript_path":
            safe[key] = value
        else:
            safe[key] = redact(value) if isinstance(value, str) else value
    return safe


def _write(session: ApplySession) -> ApplySession:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = _session_path(session.id)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps(_safe_values(asdict(session)), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return session


def create_session(*, stekkies_url: str = "", source_url: str = "",
                   source: str = "", address: str = "",
                   retry: bool = False) -> ApplySession:
    now = utc_now_iso()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    session = ApplySession(
        id=f"{stamp}-{uuid.uuid4().hex[:8]}",
        created_ts=now,
        updated_ts=now,
        stekkies_url=stekkies_url,
        source_url=source_url,
        source=source,
        address=address,
        retry=retry,
    )
    return _write(session)


def get_session(session_id: str) -> ApplySession | None:
    try:
        path = _session_path(session_id)
        return ApplySession.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def update_session(session_id: str, **changes: Any) -> ApplySession:
    current = get_session(session_id)
    if current is None:
        raise ValueError("apply session not found")
    allowed = {f.name for f in fields(ApplySession)} - {"id", "created_ts"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"unknown apply session fields: {sorted(unknown)}")
    values = asdict(current)
    values.update(_safe_values(changes))
    values["updated_ts"] = utc_now_iso()
    return _write(ApplySession.from_json(values))


def claim_session(session_id: str, *, phase: str = "preflight", **details: Any) -> ApplySession:
    return update_session(
        session_id,
        status="running",
        phase=phase,
        pid=os.getpid(),
        **{key: value for key, value in details.items() if value},
    )


def finish_session(session_id: str, *, outcome: str = "", error: str = "",
                   transcript_path: str = "") -> ApplySession:
    changes: dict[str, Any] = {
        "status": "failed" if error else "finished",
        "phase": "failed" if error else "finished",
        "outcome": outcome,
        "error": error,
    }
    # The path is normally registered before browser-lock acquisition.  Keep
    # it when an exception finishes the session without repeating the path, so
    # operators can still inspect the partial failure transcript.
    if transcript_path:
        changes["transcript_path"] = transcript_path
    return update_session(session_id, **changes)


def list_sessions(*, live_only: bool = False) -> list[ApplySession]:
    if not SESSIONS_DIR.exists():
        return []
    sessions: list[ApplySession] = []
    for path in SESSIONS_DIR.glob("*.json"):
        session = get_session(path.stem)
        if session is not None and (not live_only or session.live):
            sessions.append(session)
    return sorted(sessions, key=lambda item: item.created_ts, reverse=True)
