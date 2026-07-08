"""Cross-run memory for the self-improvement layer: incidents, not episodes.

Production data (logs/self_improvement.jsonl, 48 runs to 07-07-2026) showed
the dominant waste: the same underlying failure re-diagnosed from scratch by
several full runs — the 03-07 browser_lock hang was independently diagnosed
FIVE times in seven hours, your-house.nl's payment gate twice in one day.
Each run cost real tokens and none knew the previous ones existed.

This module gives every failure a deterministic *fingerprint* (the
`classify_failure` signature from `self_improvement_harness`, scoped to the
listing's domain only when the failure class is site-specific) and keeps an
append-only event log in `state/self_improvement/incidents.jsonl`:

- `occurrence` events: a failure matched this fingerprint (always recorded,
  even when the run is skipped — prevented spend must stay observable).
- `attempt` events: a self-improvement run happened for this fingerprint,
  with its action/root_cause/summary.

`should_run` implements the dedup policy: at most one self-improvement run
per fingerprint per `SELF_IMPROVEMENT_DEDUP_HOURS` (default 24h).
`attempt_history` feeds prior attempts into the next run's prompt so run N
starts where run N-1 stopped instead of at zero.

Everything here is fail-open: a broken store must never block or crash the
self-improvement path, let alone the apply pipeline above it.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .self_improvement_harness import STATE_DIR, classify_failure, redact_value

INCIDENT_LOG = STATE_DIR / "incidents.jsonl"

SELF_IMPROVEMENT_DEDUP_HOURS = float(os.environ.get("SELF_IMPROVEMENT_DEDUP_HOURS", "24"))

# Failure classes where the site is the story: the same signature on another
# domain is a different incident. Infrastructure classes (lock contention,
# truncated model turns, context growth) fingerprint globally — the 03-07
# lock hang surfaced across *different* listings/domains and must still
# collapse into ONE incident. `unclassified-apply-failure` is deliberately
# site-scoped too: two unknown failures on different sites are far more
# likely two problems than one, and over-collapsing them would silently skip
# a run for a genuinely new failure.
_SITE_SCOPED_SIGNATURES = {
    "payment-checkout-hard-stop",
    "cross-source-dedup",
    "refless-dialog-dom-fallback",
    "inaccessible-dialog-controls",
    "login-account-state",
    "eligibility-gate",
    "source-url-extraction",
    "unclassified-apply-failure",
}


@dataclass
class Fingerprint:
    key: str
    signature: str
    domain: str
    outcome: str


def fingerprint_failure(listing: dict, outcome: str, summary: str) -> Fingerprint:
    """Deterministic incident fingerprint for one failed apply/exception."""
    source_url = str((listing or {}).get("source_url") or "")
    sig = classify_failure(summary or "", outcome=outcome or "", domain="")
    domain = sig.domain or _domain(source_url)
    if sig.signature in _SITE_SCOPED_SIGNATURES and domain:
        key = f"{sig.signature}@{domain}"
    else:
        key = sig.signature
        domain = domain if sig.signature in _SITE_SCOPED_SIGNATURES else ""
    return Fingerprint(key=key, signature=sig.signature, domain=domain,
                       outcome=(outcome or "unknown"))


def fingerprint_poller_zero_yield(site_name: str) -> Fingerprint:
    """Incident fingerprint for a poller site that has stopped yielding
    listings (a silently-broken parser). Scoped to the site so one site's
    broken parser dedups on its own, and repeated threshold hits within the
    window don't re-diagnose it."""
    domain = _domain(site_name) or (site_name or "").strip().lower()
    return Fingerprint(key=f"poller-zero-yield@{domain}",
                       signature="poller-zero-yield", domain=domain,
                       outcome="zero_yield")


def record_occurrence(fp: Fingerprint, *, listing: dict | None = None,
                      summary: str = "", ran: bool = False) -> None:
    _append({
        "event": "occurrence",
        "fingerprint": fp.key,
        "signature": fp.signature,
        "domain": fp.domain,
        "outcome": fp.outcome,
        "source_url": str((listing or {}).get("source_url") or ""),
        "summary": (summary or "")[:600],
        "ran": bool(ran),
    })


def record_attempt(fp: Fingerprint, *, action: str, root_cause: str = "",
                   summary: str = "", code_changed: bool = False,
                   deployed: bool = False) -> None:
    _append({
        "event": "attempt",
        "fingerprint": fp.key,
        "signature": fp.signature,
        "domain": fp.domain,
        "action": action,
        "root_cause": (root_cause or "")[:600],
        "summary": (summary or "")[:600],
        "code_changed": bool(code_changed),
        "deployed": bool(deployed),
    })


def should_run(fp: Fingerprint, *, now: datetime | None = None) -> tuple[bool, str]:
    """Dedup policy: one self-improvement run per fingerprint per window.

    A deployed fix resets nothing here on purpose — if the same fingerprint
    recurs after a deploy within the window, a human should look at why the
    fix didn't land before more tokens are spent re-diagnosing.
    """
    now = now or datetime.now()
    window = timedelta(hours=SELF_IMPROVEMENT_DEDUP_HOURS)
    last = None
    for rec in _read():
        if rec.get("fingerprint") != fp.key:
            continue
        if rec.get("event") == "attempt" or (rec.get("event") == "occurrence" and rec.get("ran")):
            ts = _parse_ts(rec.get("ts"))
            if ts and (last is None or ts > last):
                last = ts
    if last is not None and (now - last) < window:
        return False, (
            f"incident {fp.key} already had a self-improvement run at "
            f"{last.isoformat(timespec='seconds')} (dedup window "
            f"{SELF_IMPROVEMENT_DEDUP_HOURS:g}h); occurrence recorded, run skipped"
        )
    return True, "no recent attempt for this fingerprint"


def attempt_history(fp: Fingerprint, *, limit: int = 3) -> list[dict[str, Any]]:
    """Most-recent-last prior attempts, for injection into the next prompt."""
    attempts = [
        {k: rec.get(k) for k in
         ("ts", "action", "root_cause", "summary", "code_changed", "deployed")}
        for rec in _read()
        if rec.get("event") == "attempt" and rec.get("fingerprint") == fp.key
    ]
    return attempts[-limit:]


def occurrence_count(fp: Fingerprint) -> int:
    return sum(
        1 for rec in _read()
        if rec.get("event") == "occurrence" and rec.get("fingerprint") == fp.key
    )


def incident_summary(*, days: float = 7.0, now: datetime | None = None) -> list[dict[str, Any]]:
    """Per-fingerprint occurrence/attempt counts over a recent window (digest)."""
    now = now or datetime.now()
    cutoff = now - timedelta(days=days)
    stats: dict[str, dict[str, Any]] = {}
    for rec in _read():
        ts = _parse_ts(rec.get("ts"))
        if ts is None or ts < cutoff:
            continue
        key = str(rec.get("fingerprint") or "?")
        row = stats.setdefault(key, {
            "fingerprint": key, "occurrences": 0, "attempts": 0,
            "skipped": 0, "deployed": 0, "last_action": "",
        })
        if rec.get("event") == "occurrence":
            row["occurrences"] += 1
            if not rec.get("ran"):
                row["skipped"] += 1
        elif rec.get("event") == "attempt":
            row["attempts"] += 1
            row["last_action"] = str(rec.get("action") or "")
            if rec.get("deployed"):
                row["deployed"] += 1
    return sorted(stats.values(), key=lambda r: -r["occurrences"])


def _append(payload: dict[str, Any]) -> None:
    try:
        INCIDENT_LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": datetime.now().isoformat(timespec="seconds"),
               **redact_value(payload, max_string=1000)}
        with INCIDENT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        return


def _read() -> list[dict[str, Any]]:
    try:
        with INCIDENT_LOG.open(encoding="utf-8") as f:
            out = []
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
            return out
    except OSError:
        return []


def _parse_ts(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _domain(url: str) -> str:
    from urllib.parse import urlparse

    host = (urlparse(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host
