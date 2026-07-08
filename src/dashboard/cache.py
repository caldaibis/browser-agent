"""Cheap caching for the read-only dashboard.

The old dashboard re-read all of `mail_summary.jsonl` (and re-parsed every
run's transcript) on every request; the overview page triggered 5+ full
reloads because `race_report`/`load_mail_events` each re-called
`load_submissions()`. These logs are append-only, so we can do far better:

- `JsonlTail` parses a JSONL file once, then on later calls only reads bytes
  appended since (keyed on size + mtime); it re-parses fully only if the file
  shrank or its inode changed (rotation/truncation). Thread-safe because
  FastAPI runs sync routes in a threadpool.
- `memo` caches a derived value for a few seconds, so the 30s/45s htmx
  polling partials and the several internal `load_submissions()` callers
  within one request collapse onto one computation.

Everything is fail-open: a missing file yields an empty record list.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable


class JsonlTail:
    """Incrementally-parsed view of an append-only JSONL file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._records: list[dict] = []
        self._offset = 0          # byte offset parsed up to
        self._size = -1
        self._mtime_ns = -1

    def records(self) -> list[dict]:
        with self._lock:
            try:
                st = self.path.stat()
            except OSError:
                # Missing/inaccessible file -> empty, and reset so a later
                # (re)appearance is picked up cleanly.
                self._records, self._offset, self._size, self._mtime_ns = [], 0, -1, -1
                return []
            if st.st_size == self._size and st.st_mtime_ns == self._mtime_ns:
                return self._records
            if st.st_size < self._offset:
                # Truncated or rotated: full reparse.
                self._records, self._offset = [], 0
            try:
                with self.path.open("rb") as f:
                    f.seek(self._offset)
                    chunk = f.read()
                    self._offset = f.tell()
            except OSError:
                return self._records
            for raw in chunk.split(b"\n"):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(rec, dict):
                    self._records.append(rec)
            self._size = st.st_size
            self._mtime_ns = st.st_mtime_ns
            return self._records


_tails: dict[str, JsonlTail] = {}
_tails_lock = threading.Lock()


def jsonl_records(path: Path) -> list[dict]:
    """Records of a JSONL file via a process-wide incremental tail cache."""
    key = str(path)
    with _tails_lock:
        tail = _tails.get(key)
        if tail is None:
            tail = _tails[key] = JsonlTail(path)
    return tail.records()


_memo: dict[str, tuple[float, Any]] = {}
_memo_lock = threading.Lock()


def memo(key: str, ttl: float, fn: Callable[[], Any]) -> Any:
    """Return `fn()`'s value, cached under `key` for `ttl` seconds."""
    now = time.monotonic()
    with _memo_lock:
        hit = _memo.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
    value = fn()
    with _memo_lock:
        _memo[key] = (now + ttl, value)
    return value


def clear() -> None:
    """Drop all cached state (tests / forced refresh)."""
    with _tails_lock:
        _tails.clear()
    with _memo_lock:
        _memo.clear()
