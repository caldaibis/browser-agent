"""Shared event logging: UTC timestamps, JSONL appends, stdout logging.

This is the ONE place that stamps persisted timestamps and the one funnel
through which event records reach disk, replacing the per-module `_log` /
`_activity` copies that used to live in orchestrator.py, poller/watcher.py,
and friends. Three invariants it enforces:

- **UTC, offset-aware timestamps** (`2026-07-08T12:34:56+00:00`). The old
  copies stamped naive local time — ambiguous at DST transitions and 2h off
  the UTC systemd journal. `parse_ts` below accepts BOTH forms forever, so
  old log/state lines keep parsing.
- **Redaction before write**: every string field goes through
  `redaction.redact`, so a site password can never reach a JSONL event file
  regardless of which module logs it.
- **One JSONL shape**: `{"ts": ..., "event": ..., **fields}`.

Appends are O_APPEND single-write, which POSIX keeps atomic for these line
sizes across the multiple writer processes (orchestrator, poller,
healthcheck).
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

from .config import LOG_DIR
from .redaction import redact

ACTIVITY_LOG = LOG_DIR / "activity.log"

_configured = False


def get_logger(name: str) -> logging.Logger:
    """Stdout logger (journald adds its own timestamps on the VPS)."""
    global _configured
    if not _configured:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
        root = logging.getLogger("stekkies")
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        root.propagate = False
        _configured = True
    return logging.getLogger(f"stekkies.{name}")


def utc_now_iso() -> str:
    """The one format persisted timestamps use: UTC, second precision."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def parse_ts(value: Any) -> datetime | None:
    """Parse any stamp this codebase ever wrote, to NAIVE LOCAL time.

    Old records are naive local; new ones are aware UTC. Normalizing both to
    naive local keeps every existing `datetime.now() - ts` age computation
    and cross-record comparison correct without touching the call sites.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _redacted_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {
        k: redact(v) if isinstance(v, str) else v
        for k, v in fields.items()
    }


def append_jsonl(path: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Append one already-shaped record; creates parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def log_event(path: Path, event: str, *, echo: str | None = None,
              **fields: Any) -> dict[str, Any]:
    """Stamp (UTC), redact, append `{"ts", "event", **fields}`, echo to stdout.

    echo: logger name for the stdout line (None = no echo).
    """
    rec = {"ts": utc_now_iso(), "event": event, **_redacted_fields(fields)}
    append_jsonl(path, rec)
    if echo:
        get_logger(echo).info(
            "%s: %s", event,
            " ".join(f"{k}={v}" for k, v in fields.items()))
    return rec


def record(path: Path, **fields: Any) -> dict[str, Any]:
    """Stamp + redact + append a record with no `event` discriminator
    (mail_summary.jsonl / processed_listings.jsonl shapes)."""
    rec = {"ts": utc_now_iso(), **_redacted_fields(fields)}
    return append_jsonl(path, rec)


def activity(message: str, *, echo: str = "activity") -> None:
    """One human-readable line in logs/activity.log (+ stdout)."""
    line = f"[{utc_now_iso()}] {redact(message)}"
    ACTIVITY_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ACTIVITY_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    get_logger(echo).info("%s", redact(message))
