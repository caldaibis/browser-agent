"""Canonical-URL dedup for cross-site listings.

The same flat surfaces on pararius + huurwoningen + a makelaar site; keying on
the tracking-stripped source URL collapses them to one apply. Seen keys are
persisted in their own JSONL and ALSO cross-checked against the orchestrator's
``state/processed_listings.jsonl`` so a listing already handled via the Stekkies
path is never re-applied by the poller.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from urllib.parse import urlparse, urlunparse

from ..config import PROJECT_ROOT

SEEN_FILE = PROJECT_ROOT / "state" / "poller_seen.jsonl"
PROCESSED_FILE = PROJECT_ROOT / "state" / "processed_listings.jsonl"

# Query params that never identify a listing — strip them before keying.
_TRACKING_PREFIXES = ("utm_", "gclid", "fbclid", "mc_")
_TRACKING_EXACT = {"ref", "referrer", "source", "src", "session", "sid", "cid"}


def canonical_url(url: str) -> str:
    """Normalize to scheme+host+path, lowercased host, no tracking query, no
    trailing slash. Query params that look like real identifiers are kept and
    sorted for stability."""
    p = urlparse(url.strip())
    host = (p.hostname or "").lower()
    scheme = p.scheme or "https"
    path = p.path.rstrip("/") or "/"

    kept = []
    for pair in p.query.split("&"):
        if not pair or "=" not in pair:
            continue
        k = pair.split("=", 1)[0].lower()
        if k in _TRACKING_EXACT or any(k.startswith(pref) for pref in _TRACKING_PREFIXES):
            continue
        kept.append(pair)
    query = "&".join(sorted(kept))

    netloc = host + (f":{p.port}" if p.port else "")
    return urlunparse((scheme, netloc, path, "", query, ""))


class SeenStore:
    """Thread-safe set of canonical URLs already seen/handled."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: set[str] = set()
        self._load()

    def _load(self) -> None:
        # Our own seen log.
        if SEEN_FILE.exists():
            for line in SEEN_FILE.read_text(encoding="utf-8").splitlines():
                try:
                    self._seen.add(json.loads(line)["key"])
                except (json.JSONDecodeError, KeyError):
                    continue
        self._load_processed()

    def _load_processed(self) -> None:
        # Anything the Stekkies pipeline already processed (dedup across paths).
        if PROCESSED_FILE.exists():
            for line in PROCESSED_FILE.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                url = rec.get("source_url")
                if url:
                    self._seen.add(canonical_url(url))

    def is_new(self, url: str) -> bool:
        with self._lock:
            self._load_processed()
            return canonical_url(url) not in self._seen

    def mark(self, url: str, **meta) -> None:
        """Record a canonical URL as seen (idempotent, persisted)."""
        key = canonical_url(url)
        with self._lock:
            if key in self._seen:
                return
            self._seen.add(key)
            SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            rec = {"ts": datetime.now().isoformat(timespec="seconds"),
                   "key": key, "url": url, **meta}
            with SEEN_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
