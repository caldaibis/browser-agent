"""Weekly outcome digest: is the pipeline actually getting better?

Nobody (human or agent) could previously tell whether a change improved the
pipeline — 5 duplicate self-improvement diagnoses in one day were only
visible after aggregating logs by hand. This builds one plain-text summary
per week from data that already exists:

- listing outcomes by status and trigger  (logs/mail_summary.jsonl)
- guard firings in the apply loop         (logs/trajectories/*.jsonl)
- self-improvement actions + landing rate (logs/self_improvement.jsonl)
- incident fingerprints and dedup savings (state/self_improvement/incidents.jsonl)
- unlanded verified fixes                 (state/pending_patches/*.patch)
- recorded known gates                    (state/known_gates.json)

`healthcheck.main` sends it via the regular alert channel once every
`DIGEST_INTERVAL_DAYS` (default 7, 0 disables); `just digest` prints it on
demand. Pure read-side: never blocks or breaks anything.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import incident_store, known_gates
from .config import LOG_DIR, PROJECT_ROOT

MAIL_SUMMARY_LOG = LOG_DIR / "mail_summary.jsonl"
SELF_IMPROVEMENT_LOG = LOG_DIR / "self_improvement.jsonl"
TRAJECTORY_DIR = LOG_DIR / "trajectories"
PENDING_PATCH_DIR = PROJECT_ROOT / "state" / "pending_patches"

_MAX_TRAJECTORY_FILES = 300


def digest_stats(*, days: float = 7.0, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now()
    cutoff = now - timedelta(days=days)
    return {
        "days": days,
        "outcomes": _outcome_stats(cutoff),
        "guards": _guard_stats(cutoff),
        "self_improvement": _self_improvement_stats(cutoff),
        "incidents": incident_store.incident_summary(days=days, now=now),
        "pending_patches": _pending_patches(),
        "known_gates": [
            {"domain": g.get("domain"), "kind": g.get("kind")}
            for g in known_gates.load_gates(now=now)
        ],
    }


def build_digest(*, days: float = 7.0, now: datetime | None = None) -> str:
    stats = digest_stats(days=days, now=now)
    lines: list[str] = [f"Rental bot digest — last {days:g} days", ""]

    lines.append("LISTING OUTCOMES (by trigger):")
    outcomes = stats["outcomes"]
    if outcomes:
        for trigger, counts in sorted(outcomes.items()):
            total = sum(counts.values())
            parts = ", ".join(f"{k}: {v}" for k, v in
                              sorted(counts.items(), key=lambda kv: -kv[1]))
            lines.append(f"  {trigger} ({total}): {parts}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("APPLY-LOOP GUARDS FIRED:")
    guards = stats["guards"]
    if guards:
        for name, count in sorted(guards.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {name}: {count}")
    else:
        lines.append("  (none recorded)")

    lines.append("")
    si = stats["self_improvement"]
    lines.append(
        f"SELF-IMPROVEMENT: {si['runs']} runs, {si['deployed']} deployed, "
        f"{si['skipped_duplicates']} duplicate incidents skipped"
    )
    for action, count in sorted(si["actions"].items(), key=lambda kv: -kv[1]):
        lines.append(f"  {action}: {count}")

    incidents = stats["incidents"]
    if incidents:
        lines.append("")
        lines.append("TOP INCIDENTS:")
        for row in incidents[:8]:
            lines.append(
                f"  {row['fingerprint']}: {row['occurrences']} occurrences, "
                f"{row['attempts']} attempts, last action: {row['last_action'] or '-'}"
            )

    pending = stats["pending_patches"]
    if pending:
        lines.append("")
        lines.append(f"⚠️ UNLANDED VERIFIED FIXES ({len(pending)}) — apply with `git am <path>`:")
        for path in pending[:10]:
            lines.append(f"  {path}")

    gates = stats["known_gates"]
    if gates:
        lines.append("")
        lines.append("ACTIVE KNOWN GATES:")
        for gate in gates:
            lines.append(f"  {gate['domain']}: {gate['kind']}")

    return "\n".join(lines)


def _outcome_stats(cutoff: datetime) -> dict[str, dict[str, int]]:
    by_trigger: dict[str, Counter] = {}
    for rec in _read_jsonl(MAIL_SUMMARY_LOG):
        ts = _parse_ts(rec.get("ts"))
        if ts is None or ts < cutoff:
            continue
        trigger = str(rec.get("trigger") or "unknown")
        status = str(rec.get("status") or "unknown")
        by_trigger.setdefault(trigger, Counter())[status] += 1
    return {k: dict(v) for k, v in by_trigger.items()}


def _guard_stats(cutoff: datetime) -> dict[str, int]:
    guards: Counter = Counter()
    try:
        paths = sorted(TRAJECTORY_DIR.glob("*.jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return {}
    for path in paths[:_MAX_TRAJECTORY_FILES]:
        try:
            if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                continue
        except OSError:
            continue
        for rec in _read_jsonl(path):
            if rec.get("event") != "guard":
                continue
            payload = rec.get("payload") or {}
            guards[str(payload.get("name") or "?")] += 1
    return dict(guards)


def _self_improvement_stats(cutoff: datetime) -> dict[str, Any]:
    actions: Counter = Counter()
    runs = deployed = skipped = 0
    for rec in _read_jsonl(SELF_IMPROVEMENT_LOG):
        ts = _parse_ts(rec.get("ts"))
        if ts is None or ts < cutoff:
            continue
        event = str(rec.get("event") or "")
        if event == "skipped_duplicate_incident":
            skipped += 1
        elif event == "error":
            runs += 1
            actions["error"] += 1
        elif event == "done":
            runs += 1
            actions[str(rec.get("action") or "unknown")] += 1
            if rec.get("deployed") in (True, "True"):
                deployed += 1
    return {"runs": runs, "deployed": deployed,
            "skipped_duplicates": skipped, "actions": dict(actions)}


def _pending_patches() -> list[str]:
    try:
        return sorted(str(p) for p in PENDING_PATCH_DIR.glob("*.patch"))
    except OSError:
        return []


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8") as f:
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


def main() -> int:
    print(build_digest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
