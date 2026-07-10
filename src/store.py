"""SQLite state store — the queryable side of `state/`.

Scope (deliberate): **state, not logs.** Processed listings + their dedup
keys, and self-improvement incidents — the records multiple processes write
and query by key. Append-only *logs* (trajectories, poller.jsonl,
mail_summary.jsonl, transcripts) stay plain files: they are only ever tailed
or scanned, and files are the right shape for that.

Why a database at all: four processes (orchestrator, poller, healthcheck,
self-improvement) appended JSONL files that every reader re-parsed in full,
and the multi-key dedup identity of a listing (source/stekkies/resolved,
raw + canonical) was re-derived by two independent code paths — which is
exactly how the duplicate REBO submission of 02-07-2026 happened. Here the
key set comes from `models.ProcessedRecord.keys()` once, and membership is
one indexed query.

Rollout safety:
- **One-time migration**: on first open (empty table + legacy JSONL
  present) the existing records are imported.
- **Dual-write**: writers append the legacy JSONL too (see callers), so
  rolling back to a pre-store build loses nothing. Drop the JSONL writes
  once a release has soaked.
- **Fail-open reads at call sites**: callers treat store errors like a
  missing file (empty set), same as the JSONL behavior before.

Keys are stored RAW; readers canonicalize at query time — canonicalization
rules still evolve (e.g. the huurwoningen.nl collapse), and re-deriving on
read is what keeps old keys matching new rules (see AGENTS.md).
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT
from .eventlog import get_logger, utc_now_iso
from .models import ProcessedRecord

DB_PATH = PROJECT_ROOT / "state" / "store.db"
LEGACY_PROCESSED = PROJECT_ROOT / "state" / "processed_listings.jsonl"
LEGACY_INCIDENTS = PROJECT_ROOT / "state" / "self_improvement" / "incidents.jsonl"

_LOG = get_logger("store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_listings (
    id      INTEGER PRIMARY KEY,
    ts      TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS listing_keys (
    key          TEXT PRIMARY KEY,
    processed_id INTEGER NOT NULL REFERENCES processed_listings(id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS incidents (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    event       TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_incidents_fingerprint ON incidents(fingerprint);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a connection (schema ensured, WAL mode, sane busy timeout).

    A fresh connection per operation keeps this trivially safe across the
    poller's worker threads and the other writer processes; at this write
    rate (a handful of records per hour) connection cost is irrelevant.
    """
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    _migrate_once(conn)
    return conn


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    """One transaction on a fresh connection, CLOSED afterwards — sqlite3's
    own context manager only commits/rolls back, it never closes."""
    conn = connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _migrate_once(conn: sqlite3.Connection) -> None:
    """Import the legacy JSONL files the first time the store comes up."""
    if conn.execute("SELECT 1 FROM processed_listings LIMIT 1").fetchone() is None \
            and LEGACY_PROCESSED.exists():
        n = 0
        for rec in _iter_jsonl(LEGACY_PROCESSED):
            _insert_processed(conn, ProcessedRecord.from_json(rec))
            n += 1
        conn.commit()
        _LOG.info(f"migrated {n} processed listings from {LEGACY_PROCESSED.name}")
    if conn.execute("SELECT 1 FROM incidents LIMIT 1").fetchone() is None \
            and LEGACY_INCIDENTS.exists():
        n = 0
        for rec in _iter_jsonl(LEGACY_INCIDENTS):
            _insert_incident(conn, rec)
            n += 1
        conn.commit()
        _LOG.info(f"migrated {n} incident events from {LEGACY_INCIDENTS.name}")


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            yield rec


# ---------------------------------------------------------------- processed --
def _insert_processed(conn: sqlite3.Connection, rec: ProcessedRecord) -> None:
    cur = conn.execute(
        "INSERT INTO processed_listings (ts, payload) VALUES (?, ?)",
        (rec.ts or utc_now_iso(), json.dumps(rec.to_json(), ensure_ascii=False)))
    rowid = cur.lastrowid
    for url in (rec.stekkies_url, rec.source_url, rec.resolved_url):
        if url:
            conn.execute(
                "INSERT OR IGNORE INTO listing_keys (key, processed_id) VALUES (?, ?)",
                (url, rowid))


def record_processed(rec: ProcessedRecord) -> None:
    with _conn() as conn:
        _insert_processed(conn, rec)


def processed_keys() -> set[str]:
    """All RAW listing URLs ever recorded as processed (source/stekkies/
    resolved). Callers canonicalize — see the module docstring."""
    with _conn() as conn:
        return {row[0] for row in conn.execute("SELECT key FROM listing_keys")}


def processed_records(limit: int | None = None) -> list[ProcessedRecord]:
    sql = "SELECT payload FROM processed_listings ORDER BY id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with _conn() as conn:
        return [ProcessedRecord.from_json(json.loads(row[0]))
                for row in conn.execute(sql)]


# ---------------------------------------------------------------- incidents --
def _insert_incident(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO incidents (ts, event, fingerprint, payload) VALUES (?, ?, ?, ?)",
        (str(payload.get("ts") or utc_now_iso()),
         str(payload.get("event") or ""),
         str(payload.get("fingerprint") or ""),
         json.dumps(payload, ensure_ascii=False)))


def record_incident(payload: dict[str, Any]) -> None:
    with _conn() as conn:
        _insert_incident(conn, payload)


def incidents(fingerprint: str | None = None) -> list[dict[str, Any]]:
    """Incident events oldest-first (the order incident_store._read had)."""
    sql, args = "SELECT payload FROM incidents", ()
    if fingerprint is not None:
        sql += " WHERE fingerprint = ?"
        args = (fingerprint,)
    sql += " ORDER BY id"
    with _conn() as conn:
        return [json.loads(row[0]) for row in conn.execute(sql, args)]
