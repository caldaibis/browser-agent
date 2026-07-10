"""Canonical-URL dedup for cross-site listings.

The same flat surfaces on pararius + huurwoningen + a makelaar site; keying on
the tracking-stripped source URL collapses them to one apply. Seen keys are
persisted in their own JSONL and ALSO cross-checked against the orchestrator's
``state/processed_listings.jsonl`` so a listing already handled via the Stekkies
path is never re-applied by the poller.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from contextlib import contextmanager
import fcntl
from urllib.parse import urlparse, urlunparse

from ..config import PROJECT_ROOT
from ..settings import settings
from ..eventlog import utc_now_iso

SEEN_FILE = PROJECT_ROOT / "state" / "poller_seen.jsonl"
PROCESSED_FILE = PROJECT_ROOT / "state" / "processed_listings.jsonl"
CLAIMS_FILE = PROJECT_ROOT / "state" / "listing_claims.jsonl"
LOCK_FILE = PROJECT_ROOT / "state" / "dedup.lock"
CLAIM_TTL_SECONDS = settings().listing_claim_ttl_seconds

# Query params that never identify a listing — strip them before keying.
_TRACKING_PREFIXES = ("utm_", "gclid", "fbclid", "mc_")
_TRACKING_EXACT = {"ref", "referrer", "source", "src", "session", "sid", "cid"}

# Site-specific canonicalization: some sites expose the SAME listing under
# several unrelated URL shapes, which path-based keying can never connect.
# Verified on huurwoningen.nl (Kaatstraat, 02-07-2026): the alert-mail
# deep-link is /frontend/listing/<full-uuid>/?alt=... while the site page
# (what Stekkies extracts and the poller discovers) is
# /huren/<city>/<uuid-first-8-hex>/<street-slug>/ — the shared listing id is
# the first UUID group. Two mails for the same flat therefore produced two
# different keys, the pre-flight duplicate check matched neither, and a full
# agent run was spent just to hit the mid-run duplicate guard. Both shapes
# collapse to a synthetic key: https://huurwoningen.nl/listing/<hex8>.
# Backward compatible: every reader re-canonicalizes stored keys/urls at load
# time, so keys written before this rule map to the new form too.
_HUURWONINGEN_LISTING_RES = (
    re.compile(r"^/frontend/listing/([0-9a-fA-F]{8})[0-9a-fA-F-]*/?"),
    re.compile(r"^/huren/[^/]+/([0-9a-fA-F]{8})(?:/|$)"),
)


def _site_listing_key(host: str, path: str) -> str | None:
    if host == "huurwoningen.nl":
        for rx in _HUURWONINGEN_LISTING_RES:
            m = rx.match(path)
            if m:
                return f"https://huurwoningen.nl/listing/{m.group(1).lower()}"
    return None


def canonical_url(url: str) -> str:
    """Normalize to scheme+host+path, lowercased host, no tracking query, no
    trailing slash. Query params that look like real identifiers are kept and
    sorted for stability. Sites listed in _site_listing_key collapse further,
    to a per-listing-id key that is stable across that site's URL shapes."""
    p = urlparse(url.strip())
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    scheme = p.scheme or "https"
    path = p.path.rstrip("/") or "/"

    site_key = _site_listing_key(host, p.path)
    if site_key:
        return site_key

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


@contextmanager
def _dedup_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _append_claim(key: str, url: str, status: str, **meta) -> None:
    CLAIMS_FILE.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": utc_now_iso(),
        "epoch": time.time(),
        "key": key,
        "url": url,
        "status": status,
        **meta,
    }
    with CLAIMS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def active_claim_keys(now: float | None = None) -> set[str]:
    """Canonical URLs currently reserved by another apply attempt.

    Claims are append-only with a TTL so a crashed poller does not block a
    listing forever.
    """
    now = now or time.time()
    active: dict[str, float] = {}
    if not CLAIMS_FILE.exists():
        return set()
    for line in CLAIMS_FILE.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = rec.get("key") or (canonical_url(rec["url"]) if rec.get("url") else "")
        key = canonical_url(key) if key else ""
        if not key:
            continue
        status = rec.get("status")
        if status == "claimed":
            epoch = float(rec.get("epoch") or 0)
            if epoch and now - epoch <= CLAIM_TTL_SECONDS:
                active[key] = epoch
            else:
                active.pop(key, None)
        elif status in {"released", "terminal"}:
            active.pop(key, None)
    return set(active)


def release_count(url: str) -> int:
    """How many times this canonical URL has already ended in a non-terminal
    (retryable) apply outcome — i.e. how many "released" claims it has.

    Used to cap automatic retries: a listing that fails the same
    non-terminal way every poll (e.g. it consistently hits the agent's turn
    budget) would otherwise be re-applied forever, burning LLM cost for a
    result that will never change.
    """
    key = canonical_url(url)
    if not CLAIMS_FILE.exists():
        return 0
    count = 0
    for line in CLAIMS_FILE.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("status") == "released" and rec.get("key") == key:
            count += 1
    return count


def known_processed_urls() -> set[str]:
    """Canonical URLs already recorded as done, from every source: the
    poller's own seen log, the Stekkies orchestrator's processed log
    (source_url/stekkies_url), and any resolved_url an apply run discovered
    mid-flight (see browser_agent.py's mid-run duplicate check).

    This is the read side of a real gap, not a hypothetical: the poller
    discovers a listing at whatever URL it found it (e.g. the huurwoningen.nl
    aggregator page), while the Stekkies-triggered flow records the FINAL
    external source URL after Stekkies' own extraction (e.g. rebogroep.nl for
    the same physical listing) -- two different canonical keys for the same
    real-world application. Reaching that final URL from a huurwoningen.nl-
    style aggregator page requires actually clicking through in-page
    redirect dialogs (not an HTTP redirect fetch.py could resolve cheaply
    up front), so this can't be checked before opening the browser. Verified
    in production (Hof van Oslo, 01-07-2026 -> 02-07-2026): the Stekkies path
    submitted successfully at 08:41 under the rebogroep.nl URL, then the
    poller re-discovered the same listing via huurwoningen.nl for hours
    afterward because that URL was never in this set -- and a manual re-test
    of the fixed agent on 02-07-2026 submitted a SECOND real application
    before this function existed. browser_agent.py calls this once per turn
    to catch the moment the browser reaches an already-known destination,
    not just before starting."""
    urls: set[str] = set()
    if SEEN_FILE.exists():
        for line in SEEN_FILE.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = rec.get("key") or rec.get("url")
            if key:
                urls.add(canonical_url(key))
    urls |= _processed_urls_canonical()
    return urls


def _processed_urls_canonical() -> set[str]:
    """Canonical keys of every processed listing: UNION of the SQLite store
    and the legacy JSONL while the store soaks (a record present in only one
    must still dedup — sets make the overlap free). Raw keys are
    canonicalized at read time so stored keys keep matching as
    canonicalization rules evolve."""
    urls: set[str] = set()
    try:
        from .. import store  # late import: store imports models imports this module

        urls |= {canonical_url(k) for k in store.processed_keys()}
    except Exception:
        pass
    if PROCESSED_FILE.exists():
        for line in PROCESSED_FILE.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            for field in ("source_url", "stekkies_url", "resolved_url"):
                value = rec.get(field)
                if value:
                    urls.add(canonical_url(value))
    return urls


class SeenStore:
    """Thread-safe set of canonical URLs already seen/handled."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: set[str] = set()
        self._reserved: set[str] = set()
        self._load()

    def _load(self) -> None:
        # Our own seen log.
        if SEEN_FILE.exists():
            for line in SEEN_FILE.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, KeyError):
                    continue
                key = rec.get("key") or rec.get("url")
                if key:
                    self._seen.add(canonical_url(key))
        self._load_processed()

    def _load_processed(self) -> None:
        # Anything the Stekkies pipeline already processed (dedup across
        # paths) -- resolved_url is the final destination an apply run
        # actually reached mid-flight (see known_processed_urls), which can
        # differ from source_url for aggregator-style listings. Includes
        # stekkies_url keys too (harmless superset: the poller never
        # discovers a listing at a stekkies.com URL).
        self._seen |= _processed_urls_canonical()

    def is_new(self, url: str) -> bool:
        with self._lock:
            self._load_processed()
            key = canonical_url(url)
            return (
                key not in self._seen
                and key not in self._reserved
                and key not in active_claim_keys()
            )

    def reserve(self, url: str, **meta) -> bool:
        """Reserve a URL before queueing/applying it.

        This closes the long window where an apply is in progress but the URL is
        not yet terminal in ``processed_listings.jsonl``. Reservations are
        process-local immediately and persisted as short-lived claims so the
        separate orchestrator process can also skip the same source URL.
        """
        key = canonical_url(url)
        with self._lock:
            with _dedup_lock():
                self._load_processed()
                if key in self._seen or key in self._reserved or key in active_claim_keys():
                    return False
                self._reserved.add(key)
                _append_claim(key, url, "claimed", **meta)
                return True

    def release(self, url: str) -> None:
        """Release a non-terminal reservation so a future poll can retry."""
        key = canonical_url(url)
        with self._lock:
            with _dedup_lock():
                self._reserved.discard(key)
                _append_claim(key, url, "released")

    def mark(self, url: str, **meta) -> None:
        """Record a canonical URL as seen (idempotent, persisted)."""
        key = canonical_url(url)
        with self._lock:
            with _dedup_lock():
                if key not in self._seen:
                    self._seen.add(key)
                    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
                    rec = {"ts": utc_now_iso(),
                           "key": key, "url": url, **meta}
                    with SEEN_FILE.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                self._reserved.discard(key)
                _append_claim(key, url, "terminal", **meta)
