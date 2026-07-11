"""Durable, single-consumer queue for self-improvement work.

Apply/orchestrator processes only persist evidence.  A separate systemd timer
drains this queue, so a self-improvement push/deploy cannot kill the process
that reported the original listing and concurrent failures cannot race git
worktrees or move ``origin/main`` underneath each other.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from . import eventlog
from .config import PROJECT_ROOT
from .redaction import redact

QUEUE_ROOT = PROJECT_ROOT / "state" / "self_improvement" / "queue"
PENDING_DIR = QUEUE_ROOT / "pending"
RUNNING_DIR = QUEUE_ROOT / "running"
FAILED_DIR = QUEUE_ROOT / "failed"
RUN_LOCK = QUEUE_ROOT / "worker.lock"


def enqueue(kind: str, payload: dict[str, Any]) -> str:
    """Atomically persist one redacted job and return its stable id."""
    job_id = f"{int(time.time()):010d}-{uuid.uuid4().hex[:12]}"
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": "self-improvement-job-v1",
        "job_id": job_id,
        "created_ts": eventlog.utc_now_iso(),
        "kind": kind,
        "payload": _safe_payload(payload),
    }
    tmp = PENDING_DIR / f".{job_id}.tmp"
    final = PENDING_DIR / f"{job_id}.json"
    tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, final)
    return job_id


@contextmanager
def worker_lock() -> Iterator[bool]:
    """Yield whether this process became the sole queue consumer."""
    QUEUE_ROOT.mkdir(parents=True, exist_ok=True)
    fd = os.open(RUN_LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def recover_orphans() -> int:
    """Return jobs left in ``running`` by a killed worker to the queue."""
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    RUNNING_DIR.mkdir(parents=True, exist_ok=True)
    recovered = 0
    for path in sorted(RUNNING_DIR.glob("*.json")):
        target = PENDING_DIR / path.name
        if target.exists():
            target = PENDING_DIR / f"recovered-{uuid.uuid4().hex[:8]}-{path.name}"
        os.replace(path, target)
        recovered += 1
    return recovered


def claim_next() -> tuple[Path, dict[str, Any]] | None:
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    RUNNING_DIR.mkdir(parents=True, exist_ok=True)
    for source in sorted(PENDING_DIR.glob("*.json")):
        target = RUNNING_DIR / source.name
        try:
            os.replace(source, target)
        except FileNotFoundError:
            continue
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            fail(target, "invalid queued JSON")
            continue
        if isinstance(data, dict):
            return target, data
        fail(target, "queued value is not an object")
    return None


def complete(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def fail(path: Path, reason: str) -> None:
    FAILED_DIR.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data["failed_ts"] = eventlog.utc_now_iso()
            data["failure"] = redact(reason)[:3000]
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    target = FAILED_DIR / path.name
    if target.exists():
        target = FAILED_DIR / f"{uuid.uuid4().hex[:8]}-{path.name}"
    try:
        os.replace(path, target)
    except FileNotFoundError:
        pass


def queue_counts() -> dict[str, int]:
    return {
        "pending": _count(PENDING_DIR),
        "running": _count(RUNNING_DIR),
        "failed": _count(FAILED_DIR),
    }


def _count(path: Path) -> int:
    try:
        return sum(1 for _ in path.glob("*.json"))
    except OSError:
        return 0


def _safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    # Queue files are durable state and may be displayed/attached later.  Use
    # the same central redactor before the first write, not at read time.
    from .self_improvement_harness import redact_value

    safe = redact_value(payload, max_string=30_000)
    return safe if isinstance(safe, dict) else {"value": safe}
